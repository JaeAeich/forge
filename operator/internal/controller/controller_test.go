package controller

import (
	"context"
	"testing"
	"time"

	snapv1 "github.com/kubernetes-csi/external-snapshotter/client/v8/apis/volumesnapshot/v1"
	. "github.com/onsi/gomega"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/rand"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	sbv1 "forge.dev/operator/api/v1alpha1"
)

var ctx = context.Background()

func namespace(t *testing.T) string {
	requireAPIServer(t)
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "t-" + rand.String(6)}}
	NewWithT(t).Expect(k8s.Create(ctx, ns)).To(Succeed())
	return ns.Name
}

func newSandbox(ns, name string) *sbv1.Sandbox {
	return &sbv1.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: sbv1.SandboxSpec{Containers: []sbv1.Container{
			{Name: "main", Image: "forge-sandbox:1", Command: []string{"sleep", "infinity"}, Volume: &sbv1.Volume{MountPath: "/repo"}},
		}},
	}
}

// get polls an object; Eventually retries until the Get succeeds and the matcher passes.
func get[T client.Object](ns, name string, obj T) func() (T, error) {
	return func() (T, error) { return obj, k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, obj) }
}

// runPod plays the kubelet: marks every container of the sandbox's pod running and ready.
func runPod(g Gomega, ns, name string) {
	var pod corev1.Pod
	g.Eventually(func() error { return k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &pod) }).Should(Succeed())
	pod.Status.Phase = corev1.PodRunning
	pod.Status.ContainerStatuses = nil
	for _, c := range pod.Spec.Containers {
		pod.Status.ContainerStatuses = append(pod.Status.ContainerStatuses, corev1.ContainerStatus{
			Name: c.Name, Image: c.Image, Ready: true, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}},
		})
	}
	g.Expect(k8s.Status().Update(ctx, &pod)).To(Succeed())
}

// cutVolumeSnapshot plays the CSI snapshotter.
func cutVolumeSnapshot(g Gomega, ns, name string, ready bool) {
	var vs snapv1.VolumeSnapshot
	g.Eventually(func() error { return k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &vs) }).Should(Succeed())
	vs.Status = &snapv1.VolumeSnapshotStatus{CreationTime: ptr.To(metav1.Now()), ReadyToUse: ptr.To(ready)}
	g.Expect(k8s.Status().Update(ctx, &vs)).To(Succeed())
}

func TestSandboxLifecycle(t *testing.T) {
	g := NewWithT(t)
	ns := namespace(t)
	g.Expect(k8s.Create(ctx, newSandbox(ns, "box"))).To(Succeed())

	// children appear, owned by the sandbox
	var pod corev1.Pod
	g.Eventually(get(ns, "box", &pod)).Should(WithTransform(func(p *corev1.Pod) string { return p.Name }, Equal("box")))
	g.Expect(pod.OwnerReferences).To(HaveLen(1))
	g.Expect(*pod.Spec.AutomountServiceAccountToken).To(BeFalse())
	var pvc corev1.PersistentVolumeClaim
	g.Eventually(func() error { return k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "box-main"}, &pvc) }).Should(Succeed())
	var np networkingv1.NetworkPolicy
	g.Eventually(func() error { return k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "box"}, &np) }).Should(Succeed())
	g.Expect(np.Spec.Ingress).To(BeEmpty())
	g.Expect(np.Spec.Egress).To(BeEmpty())

	var sb sbv1.Sandbox
	g.Eventually(get(ns, "box", &sb)).Should(WithTransform(func(s *sbv1.Sandbox) sbv1.SandboxPhase { return s.Status.Phase }, Equal(sbv1.SandboxPending)))

	runPod(g, ns, "box")
	g.Eventually(get(ns, "box", &sb)).Should(WithTransform(func(s *sbv1.Sandbox) sbv1.SandboxPhase { return s.Status.Phase }, Equal(sbv1.SandboxRunning)))
	g.Expect(sb.Status.ReadyAt).NotTo(BeNil())
	g.Expect(sb.Status.Template).To(Equal(sb.Spec.Containers))
}

func TestSnapshotAndRestore(t *testing.T) {
	g := NewWithT(t)
	ns := namespace(t)
	g.Expect(k8s.Create(ctx, newSandbox(ns, "src"))).To(Succeed())
	runPod(g, ns, "src")

	snap := &sbv1.SandboxSnapshot{ObjectMeta: metav1.ObjectMeta{Name: "s1", Namespace: ns}, Spec: sbv1.SandboxSnapshotSpec{SandboxName: "src"}}
	g.Expect(k8s.Create(ctx, snap)).To(Succeed())

	// a restore requested before the snapshot is ready waits for it
	restore := &sbv1.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: "fork", Namespace: ns},
		Spec:       sbv1.SandboxSpec{SnapshotRef: &corev1.LocalObjectReference{Name: "s1"}},
	}
	g.Expect(k8s.Create(ctx, restore)).To(Succeed())

	phase := func(s *sbv1.SandboxSnapshot) sbv1.SnapshotPhase { return s.Status.Phase }
	cutVolumeSnapshot(g, ns, "s1-main", false)
	g.Eventually(get(ns, "s1", snap)).Should(WithTransform(phase, Equal(sbv1.SnapshotCut)))
	g.Expect(snap.Status.CutAt).NotTo(BeNil())
	g.Consistently(func() bool {
		return apierrors.IsNotFound(k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "fork-main"}, &corev1.PersistentVolumeClaim{}))
	}, "500ms").Should(BeTrue(), "restore must wait until the snapshot is restorable")

	cutVolumeSnapshot(g, ns, "s1-main", true)
	g.Eventually(get(ns, "s1", snap)).Should(WithTransform(phase, Equal(sbv1.SnapshotReady)))
	g.Expect(snap.Status.Template).To(HaveLen(1))
	g.Expect(snap.Status.Volumes).To(ConsistOf(sbv1.VolumeSnapshotRef{Container: "main", VolumeSnapshot: "s1-main"}))

	var pvc corev1.PersistentVolumeClaim
	g.Eventually(func() error { return k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "fork-main"}, &pvc) }).Should(Succeed())
	g.Expect(pvc.Spec.DataSource).NotTo(BeNil())
	g.Expect(pvc.Spec.DataSource.Name).To(Equal("s1-main"))

	// the snapshot outlives its sandbox
	var vs snapv1.VolumeSnapshot
	g.Expect(k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "s1-main"}, &vs)).To(Succeed())
	g.Expect(vs.OwnerReferences[0].Kind).To(Equal("SandboxSnapshot"))
}

func TestSnapshotFailures(t *testing.T) {
	g := NewWithT(t)
	ns := namespace(t)
	phase := func(s *sbv1.SandboxSnapshot) sbv1.SnapshotPhase { return s.Status.Phase }

	orphan := &sbv1.SandboxSnapshot{ObjectMeta: metav1.ObjectMeta{Name: "orphan", Namespace: ns}, Spec: sbv1.SandboxSnapshotSpec{SandboxName: "nope"}}
	g.Expect(k8s.Create(ctx, orphan)).To(Succeed())
	g.Eventually(get(ns, "orphan", orphan)).Should(WithTransform(phase, Equal(sbv1.SnapshotFailed)))

	// restoring a failed snapshot fails the sandbox instead of waiting forever
	restore := &sbv1.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: "r", Namespace: ns},
		Spec:       sbv1.SandboxSpec{SnapshotRef: &corev1.LocalObjectReference{Name: "orphan"}},
	}
	g.Expect(k8s.Create(ctx, restore)).To(Succeed())
	g.Eventually(get(ns, "r", restore)).Should(WithTransform(func(s *sbv1.Sandbox) sbv1.SandboxPhase { return s.Status.Phase }, Equal(sbv1.SandboxFailed)))
}

func TestTTLDeletesSandbox(t *testing.T) {
	g := NewWithT(t)
	ns := namespace(t)
	sb := newSandbox(ns, "short")
	sb.Spec.TTL = &metav1.Duration{Duration: time.Minute}
	g.Expect(k8s.Create(ctx, sb)).To(Succeed())
	g.Eventually(get(ns, "short", sb)).Should(WithTransform(func(s *sbv1.Sandbox) *metav1.Time { return s.Status.ExpiresAt }, Not(BeNil())))

	clock.Advance(2 * time.Minute)
	sb.Spec.TTL = &metav1.Duration{Duration: 61 * time.Second} // any update triggers a reconcile (TTL is the mutable field)
	g.Expect(k8s.Update(ctx, sb)).To(Succeed())
	g.Eventually(func() bool {
		return apierrors.IsNotFound(k8s.Get(ctx, client.ObjectKeyFromObject(sb), &sbv1.Sandbox{}))
	}).Should(BeTrue())
}

func TestValidation(t *testing.T) {
	g := NewWithT(t)
	ns := namespace(t)

	both := newSandbox(ns, "both")
	both.Spec.SnapshotRef = &corev1.LocalObjectReference{Name: "x"}
	g.Expect(k8s.Create(ctx, both)).To(MatchError(ContainSubstring("exactly one of containers or snapshotRef")))

	sb := newSandbox(ns, "immutable")
	g.Expect(k8s.Create(ctx, sb)).To(Succeed())
	sb.Spec.Containers[0].Image = "other:2"
	g.Expect(k8s.Update(ctx, sb)).To(MatchError(ContainSubstring("immutable")))
}
