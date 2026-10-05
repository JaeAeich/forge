package controller

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	snapv1 "github.com/kubernetes-csi/external-snapshotter/client/v8/apis/volumesnapshot/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	sbv1 "forge.dev/operator/api/v1alpha1"
)

// Integration tests run the real controllers against a real API server (envtest: kube-apiserver + etcd, no
// kubelet). Pods never actually start, so tests play the kubelet / CSI snapshotter by writing statuses.

var (
	k8s   client.Client
	clock = &fakeClock{t: time.Now()}
)

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func requireAPIServer(t *testing.T) {
	if k8s == nil {
		t.Skip("envtest binaries unavailable")
	}
}

func TestMain(m *testing.M) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		out, err := exec.Command("go", "tool", "setup-envtest", "use", "-p", "path").Output()
		if err != nil {
			println("envtest binaries unavailable, integration tests will skip:", err.Error())
			os.Exit(m.Run())
		}
		os.Setenv("KUBEBUILDER_ASSETS", strings.TrimSpace(string(out)))
	}
	env := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "deploy", "operator", "crd"), "testdata"},
		ErrorIfCRDPathMissing: true,
	}
	cfg, err := env.Start()
	if err != nil {
		panic(err)
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, snapv1.AddToScheme, sbv1.AddToScheme} {
		if err := add(scheme); err != nil {
			panic(err)
		}
	}
	mgr, err := ctrl.NewManager(cfg, ctrl.Options{Scheme: scheme, Metrics: metricsserver.Options{BindAddress: "0"}})
	if err != nil {
		panic(err)
	}
	if err := (&SandboxReconciler{Client: mgr.GetClient(), Scheme: scheme, Recorder: mgr.GetEventRecorder("t"), Now: clock.Now}).SetupWithManager(mgr); err != nil {
		panic(err)
	}
	if err := (&SnapshotReconciler{Client: mgr.GetClient(), Scheme: scheme, Recorder: mgr.GetEventRecorder("t"), Now: clock.Now}).SetupWithManager(mgr); err != nil {
		panic(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		if err := mgr.Start(ctx); err != nil {
			panic(err)
		}
	}()
	if k8s, err = client.New(cfg, client.Options{Scheme: scheme}); err != nil {
		panic(err)
	}

	code := m.Run()
	cancel()
	_ = env.Stop()
	os.Exit(code)
}
