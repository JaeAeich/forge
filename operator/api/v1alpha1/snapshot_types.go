package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SandboxSnapshotSpec names the sandbox to capture. A snapshot is immutable once taken.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="spec is immutable"
type SandboxSnapshotSpec struct {
	// Sandbox in the same namespace. It must be Running.
	// +kubebuilder:validation:MinLength=1
	SandboxName string `json:"sandboxName"`
	// VolumeSnapshotClass to use; the cluster default if empty.
	// +optional
	VolumeSnapshotClassName *string `json:"volumeSnapshotClassName,omitempty"`
}

// SnapshotPhase is a coarse lifecycle summary.
//
//	Pending -> Cut   every volume's point-in-time image is taken; the sandbox may keep writing.
//	Cut     -> Ready every volume can be restored from.
//
// +kubebuilder:validation:Enum=Pending;Cut;Ready;Failed
type SnapshotPhase string

const (
	SnapshotPending SnapshotPhase = "Pending"
	SnapshotCut     SnapshotPhase = "Cut"
	SnapshotReady   SnapshotPhase = "Ready"
	SnapshotFailed  SnapshotPhase = "Failed"
)

// VolumeSnapshotRef ties a container's volume to the VolumeSnapshot that holds it.
type VolumeSnapshotRef struct {
	Container      string `json:"container"`
	VolumeSnapshot string `json:"volumeSnapshot"`
}

// SandboxSnapshotStatus is written by the controller only.
type SandboxSnapshotStatus struct {
	// +optional
	Phase SnapshotPhase `json:"phase,omitempty"`
	// The sandbox's containers at capture time; restores run exactly these.
	// +optional
	Template []Container `json:"template,omitempty"`
	// +optional
	Volumes []VolumeSnapshotRef `json:"volumes,omitempty"`
	// +optional
	CutAt *metav1.Time `json:"cutAt,omitempty"`
	// +optional
	ReadyAt *metav1.Time `json:"readyAt,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=sbxsnap,categories=forge
// +kubebuilder:printcolumn:name="Sandbox",type=string,JSONPath=`.spec.sandboxName`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// SandboxSnapshot is a point-in-time copy of every volume of a Sandbox. It outlives the sandbox.
type SandboxSnapshot struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SandboxSnapshotSpec   `json:"spec"`
	Status SandboxSnapshotStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// SandboxSnapshotList is a list of SandboxSnapshots.
type SandboxSnapshotList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []SandboxSnapshot `json:"items"`
}

func init() {
	SchemeBuilder.Register(&SandboxSnapshot{}, &SandboxSnapshotList{})
}
