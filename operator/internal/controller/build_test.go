package controller

import (
	"testing"

	. "github.com/onsi/gomega"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	sbv1 "forge.dev/operator/api/v1alpha1"
)

func stack() (*sbv1.Sandbox, []sbv1.Container) {
	sb := &sbv1.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: "env", Namespace: "ns"}}
	return sb, []sbv1.Container{
		{Name: "app", Image: "app:1"},
		{Name: "db", Image: "postgres:16", Volume: &sbv1.Volume{MountPath: "/pgdata"}},
	}
}

func TestPodSharesNetworkAndMountsVolumes(t *testing.T) {
	g := NewWithT(t)
	sb, tpl := stack()
	pod := buildPod(sb, tpl)

	g.Expect(pod.Spec.HostAliases).To(ConsistOf(corev1.HostAlias{IP: "127.0.0.1", Hostnames: []string{"app", "db"}}))
	g.Expect(pod.Spec.RestartPolicy).To(Equal(corev1.RestartPolicyNever))
	g.Expect(*pod.Spec.AutomountServiceAccountToken).To(BeFalse())
	g.Expect(pod.Labels).To(HaveKeyWithValue(LabelSandbox, "env"))
	g.Expect(pod.Spec.Containers[0].VolumeMounts).To(BeEmpty())
	g.Expect(pod.Spec.Containers[1].VolumeMounts).To(ConsistOf(corev1.VolumeMount{Name: "vol-db", MountPath: "/pgdata"}))
	g.Expect(pod.Spec.Volumes).To(HaveLen(1))
	g.Expect(pod.Spec.Volumes[0].PersistentVolumeClaim.ClaimName).To(Equal("env-db"))
}

func TestPVCRestoresFromVolumeSnapshot(t *testing.T) {
	g := NewWithT(t)
	sb, tpl := stack()

	fresh := buildPVC(sb, tpl[1], "")
	g.Expect(fresh.Spec.DataSource).To(BeNil())
	g.Expect(fresh.Spec.Resources.Requests.Storage().String()).To(Equal(DefaultSize))

	restored := buildPVC(sb, tpl[1], "snap-db")
	g.Expect(restored.Spec.DataSource.Kind).To(Equal("VolumeSnapshot"))
	g.Expect(restored.Spec.DataSource.Name).To(Equal("snap-db"))
}

func TestPodState(t *testing.T) {
	ready := func(ok bool) corev1.ContainerStatus {
		return corev1.ContainerStatus{Name: "c", Ready: ok, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}}
	}
	for name, tc := range map[string]struct {
		pod  corev1.PodStatus
		want sbv1.SandboxPhase
	}{
		"scheduling": {corev1.PodStatus{Phase: corev1.PodPending}, sbv1.SandboxPending},
		"all ready":  {corev1.PodStatus{Phase: corev1.PodRunning, ContainerStatuses: []corev1.ContainerStatus{ready(true)}}, sbv1.SandboxRunning},
		"one not":    {corev1.PodStatus{Phase: corev1.PodRunning, ContainerStatuses: []corev1.ContainerStatus{ready(true), ready(false)}}, sbv1.SandboxPending},
		"exited": {corev1.PodStatus{Phase: corev1.PodRunning, ContainerStatuses: []corev1.ContainerStatus{
			{Name: "db", State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{Reason: "OOMKilled"}}},
		}}, sbv1.SandboxFailed},
		"failed": {corev1.PodStatus{Phase: corev1.PodFailed}, sbv1.SandboxFailed},
	} {
		t.Run(name, func(t *testing.T) {
			got, _ := podState(&corev1.Pod{Status: tc.pod})
			NewWithT(t).Expect(got).To(Equal(tc.want))
		})
	}
}
