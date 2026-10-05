package controller

import (
	snapv1 "github.com/kubernetes-csi/external-snapshotter/client/v8/apis/volumesnapshot/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	sbv1 "forge.dev/operator/api/v1alpha1"
)

// Pure functions: desired child objects for a Sandbox / SandboxSnapshot. No API calls, easy to unit test.

const (
	LabelSandbox   = "sandbox.forge.dev/name"
	LabelManagedBy = "app.kubernetes.io/managed-by"
	ManagedBy      = "forge-sandbox-operator"
	DefaultSize    = "2Gi"
)

func labels(sb *sbv1.Sandbox) map[string]string {
	return map[string]string{LabelSandbox: sb.Name, LabelManagedBy: ManagedBy}
}

func pvcName(sandbox, container string) string { return sandbox + "-" + container }

func volumeSnapshotName(snapshot, container string) string { return snapshot + "-" + container }

func volumeName(container string) string { return "vol-" + container }

// buildPod runs every container of the template in one pod: shared network namespace, each container name
// resolvable as 127.0.0.1 (the same addressing a Docker network with aliases gives locally).
func buildPod(sb *sbv1.Sandbox, template []sbv1.Container) *corev1.Pod {
	hostnames := make([]string, 0, len(template))
	containers := make([]corev1.Container, 0, len(template))
	var volumes []corev1.Volume
	for _, c := range template {
		hostnames = append(hostnames, c.Name)
		ctr := corev1.Container{
			Name:       c.Name,
			Image:      c.Image,
			Command:    c.Command,
			Args:       c.Args,
			Env:        c.Env,
			WorkingDir: c.WorkingDir,
			Resources:  c.Resources,
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"NET_RAW"}},
			},
		}
		if c.Volume != nil {
			ctr.VolumeMounts = []corev1.VolumeMount{{Name: volumeName(c.Name), MountPath: c.Volume.MountPath}}
			volumes = append(volumes, corev1.Volume{
				Name: volumeName(c.Name),
				VolumeSource: corev1.VolumeSource{
					PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: pvcName(sb.Name, c.Name)},
				},
			})
		}
		containers = append(containers, ctr)
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: sb.Name, Namespace: sb.Namespace, Labels: labels(sb)},
		Spec: corev1.PodSpec{
			Containers:                    containers,
			Volumes:                       volumes,
			RestartPolicy:                 corev1.RestartPolicyNever, // a restart would silently lose in-memory state
			RuntimeClassName:              sb.Spec.RuntimeClassName,
			HostAliases:                   []corev1.HostAlias{{IP: "127.0.0.1", Hostnames: hostnames}},
			AutomountServiceAccountToken:  ptr.To(false),
			EnableServiceLinks:            ptr.To(false),
			TerminationGracePeriodSeconds: ptr.To[int64](5),
			SecurityContext: &corev1.PodSecurityContext{
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
		},
	}
}

// buildPVC is a container's volume: empty for a fresh sandbox, cloned from a VolumeSnapshot on restore.
func buildPVC(sb *sbv1.Sandbox, c sbv1.Container, fromVolumeSnapshot string) *corev1.PersistentVolumeClaim {
	size := resource.MustParse(DefaultSize)
	if c.Volume.Size != nil {
		size = *c.Volume.Size
	}
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: pvcName(sb.Name, c.Name), Namespace: sb.Namespace, Labels: labels(sb)},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			StorageClassName: sb.Spec.StorageClassName,
			Resources:        corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: size}},
		},
	}
	if fromVolumeSnapshot != "" {
		pvc.Spec.DataSource = &corev1.TypedLocalObjectReference{
			APIGroup: ptr.To(snapv1.GroupName),
			Kind:     "VolumeSnapshot",
			Name:     fromVolumeSnapshot,
		}
	}
	return pvc
}

// buildNetworkPolicy selects the sandbox pod and allows nothing in or out.
func buildNetworkPolicy(sb *sbv1.Sandbox) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: sb.Name, Namespace: sb.Namespace, Labels: labels(sb)},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{LabelSandbox: sb.Name}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress},
		},
	}
}

func buildVolumeSnapshot(snap *sbv1.SandboxSnapshot, container string) *snapv1.VolumeSnapshot {
	return &snapv1.VolumeSnapshot{
		ObjectMeta: metav1.ObjectMeta{
			Name:      volumeSnapshotName(snap.Name, container),
			Namespace: snap.Namespace,
			Labels:    map[string]string{LabelManagedBy: ManagedBy},
		},
		Spec: snapv1.VolumeSnapshotSpec{
			Source:                  snapv1.VolumeSnapshotSource{PersistentVolumeClaimName: ptr.To(pvcName(snap.Spec.SandboxName, container))},
			VolumeSnapshotClassName: snap.Spec.VolumeSnapshotClassName,
		},
	}
}

// podState summarises a sandbox pod: Running once every container is up.
func podState(pod *corev1.Pod) (sbv1.SandboxPhase, string) {
	switch pod.Status.Phase {
	case corev1.PodFailed, corev1.PodSucceeded:
		return sbv1.SandboxFailed, firstTermination(pod)
	case corev1.PodRunning:
		for _, cs := range pod.Status.ContainerStatuses {
			if cs.State.Terminated != nil {
				return sbv1.SandboxFailed, firstTermination(pod)
			}
			if !cs.Ready {
				return sbv1.SandboxPending, "container " + cs.Name + " not ready"
			}
		}
		return sbv1.SandboxRunning, ""
	}
	for _, cs := range pod.Status.ContainerStatuses {
		if w := cs.State.Waiting; w != nil && w.Reason != "" && w.Reason != "ContainerCreating" {
			return sbv1.SandboxPending, cs.Name + ": " + w.Reason + " " + w.Message
		}
	}
	return sbv1.SandboxPending, "pod " + string(pod.Status.Phase)
}

func firstTermination(pod *corev1.Pod) string {
	for _, cs := range pod.Status.ContainerStatuses {
		if t := cs.State.Terminated; t != nil {
			return cs.Name + " exited: " + t.Reason + " " + t.Message
		}
	}
	return "pod " + string(pod.Status.Phase)
}
