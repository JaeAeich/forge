package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// NetworkMode controls what a sandbox can reach.
// +kubebuilder:validation:Enum=None;Cluster
type NetworkMode string

const (
	// NetworkNone denies all ingress and egress (agent code runs here).
	NetworkNone NetworkMode = "None"
	// NetworkCluster applies no extra policy.
	NetworkCluster NetworkMode = "Cluster"
)

// Volume is a directory that lives on its own PersistentVolumeClaim. Volumes are what snapshots capture.
type Volume struct {
	// Where the volume is mounted in the container.
	// +kubebuilder:validation:Pattern=`^/`
	MountPath string `json:"mountPath"`
	// +kubebuilder:default="2Gi"
	// +optional
	Size *resource.Quantity `json:"size,omitempty"`
}

// Container is one process in a sandbox. Containers of the same sandbox share a network namespace
// and reach each other by name (each name resolves to 127.0.0.1).
type Container struct {
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	// +kubebuilder:validation:MaxLength=40
	Name  string `json:"name"`
	Image string `json:"image"`
	// +optional
	Command []string `json:"command,omitempty"`
	// +optional
	Args []string `json:"args,omitempty"`
	// +optional
	Env []corev1.EnvVar `json:"env,omitempty"`
	// +optional
	WorkingDir string `json:"workingDir,omitempty"`
	// +optional
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`
	// +optional
	Volume *Volume `json:"volume,omitempty"`
}

// SandboxSpec describes an isolated, snapshot-able environment.
// Exactly one of containers or snapshotRef is set: a fresh sandbox, or a restore of a snapshot.
// +kubebuilder:validation:XValidation:rule="has(self.containers) != has(self.snapshotRef)",message="set exactly one of containers or snapshotRef"
type SandboxSpec struct {
	// +optional
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="immutable"
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=8
	Containers []Container `json:"containers,omitempty"`

	// Restore every volume (and the container template) from this SandboxSnapshot in the same namespace.
	// +optional
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="immutable"
	SnapshotRef *corev1.LocalObjectReference `json:"snapshotRef,omitempty"`

	// +kubebuilder:default=None
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="immutable"
	// +optional
	Network NetworkMode `json:"network,omitempty"`

	// RuntimeClass for the pod, e.g. gvisor or kata. Agent code is untrusted.
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="immutable"
	// +optional
	RuntimeClassName *string `json:"runtimeClassName,omitempty"`

	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="immutable"
	// +optional
	StorageClassName *string `json:"storageClassName,omitempty"`

	// The sandbox deletes itself this long after creation, so a crashed client never leaks one.
	// The only mutable field: raise it to keep a long-running sandbox alive.
	// +kubebuilder:default="2h"
	// +optional
	TTL *metav1.Duration `json:"ttl,omitempty"`
}

// SandboxPhase is a coarse lifecycle summary.
// +kubebuilder:validation:Enum=Pending;Running;Failed
type SandboxPhase string

const (
	SandboxPending SandboxPhase = "Pending"
	SandboxRunning SandboxPhase = "Running"
	SandboxFailed  SandboxPhase = "Failed"
)

// SandboxStatus is written by the controller only.
type SandboxStatus struct {
	// +optional
	Phase SandboxPhase `json:"phase,omitempty"`
	// Pod that runs the containers; exec into it.
	// +optional
	PodName string `json:"podName,omitempty"`
	// The containers actually running: spec.containers, or the snapshot's template on restore.
	// +optional
	Template []Container `json:"template,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	ReadyAt *metav1.Time `json:"readyAt,omitempty"`
	// +optional
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=sbx,categories=forge
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Pod",type=string,JSONPath=`.status.podName`
// +kubebuilder:printcolumn:name="From",type=string,JSONPath=`.spec.snapshotRef.name`
// +kubebuilder:printcolumn:name="Expires",type=date,JSONPath=`.status.expiresAt`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
// +kubebuilder:validation:XValidation:rule="self.metadata.name.size() <= 63",message="name must be at most 63 characters (it names the pod and labels)"

// Sandbox is a pod of one or more containers with persistent volumes that can be snapshotted and forked.
type Sandbox struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SandboxSpec   `json:"spec"`
	Status SandboxStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// SandboxList is a list of Sandboxes.
type SandboxList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Sandbox `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Sandbox{}, &SandboxList{})
}
