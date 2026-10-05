// Command operator runs the forge sandbox controllers (Sandbox, SandboxSnapshot).
package main

import (
	"flag"
	"os"
	"time"

	snapv1 "github.com/kubernetes-csi/external-snapshotter/client/v8/apis/volumesnapshot/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	sbv1 "forge.dev/operator/api/v1alpha1"
	"forge.dev/operator/internal/controller"
)

func main() {
	var metricsAddr, probeAddr, namespace string
	var leaderElect bool
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "Prometheus metrics endpoint.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "Liveness/readiness endpoint.")
	flag.StringVar(&namespace, "namespace", "", "Only watch this namespace (default: all).")
	flag.BoolVar(&leaderElect, "leader-elect", true, "Run one active replica at a time.")
	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	log := ctrl.Log.WithName("setup")

	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(snapv1.AddToScheme(scheme))
	utilruntime.Must(sbv1.AddToScheme(scheme))

	var cacheOpts cache.Options
	if namespace != "" {
		cacheOpts.DefaultNamespaces = map[string]cache.Config{namespace: {}}
	}
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                        scheme,
		Cache:                         cacheOpts,
		Metrics:                       metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress:        probeAddr,
		LeaderElection:                leaderElect,
		LeaderElectionID:              "sandbox.forge.dev",
		LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		log.Error(err, "create manager")
		os.Exit(1)
	}

	if err := (&controller.SandboxReconciler{
		Client: mgr.GetClient(), Scheme: scheme, Recorder: mgr.GetEventRecorder("sandbox"), Now: time.Now,
	}).SetupWithManager(mgr); err != nil {
		log.Error(err, "setup sandbox controller")
		os.Exit(1)
	}
	if err := (&controller.SnapshotReconciler{
		Client: mgr.GetClient(), Scheme: scheme, Recorder: mgr.GetEventRecorder("sandboxsnapshot"), Now: time.Now,
	}).SetupWithManager(mgr); err != nil {
		log.Error(err, "setup snapshot controller")
		os.Exit(1)
	}
	utilruntime.Must(mgr.AddHealthzCheck("healthz", healthz.Ping))
	utilruntime.Must(mgr.AddReadyzCheck("readyz", healthz.Ping))

	log.Info("starting")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		log.Error(err, "manager exited")
		os.Exit(1)
	}
}
