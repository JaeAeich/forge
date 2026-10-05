package controller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/events"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	sbv1 "forge.dev/operator/api/v1alpha1"
)

const (
	DefaultTTL       = 2 * time.Hour
	indexSnapshotRef = ".spec.snapshotRef.name"
	condReady        = "Ready"
)

// SandboxReconciler turns a Sandbox into a Pod, one PVC per volume and (for Network=None) a deny-all
// NetworkPolicy, all owned by the Sandbox so deleting it cleans everything up. Restores wait for the
// referenced SandboxSnapshot to be Ready, then clone each volume from its VolumeSnapshot.
type SandboxReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder events.EventRecorder
	Now      func() time.Time
}

// +kubebuilder:rbac:groups=sandbox.forge.dev,resources=sandboxes,verbs=get;list;watch;update;patch;delete
// +kubebuilder:rbac:groups=sandbox.forge.dev,resources=sandboxes/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=sandbox.forge.dev,resources=sandboxes/finalizers,verbs=update
// +kubebuilder:rbac:groups=sandbox.forge.dev,resources=sandboxsnapshots,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=pods;persistentvolumeclaims,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=events.k8s.io,resources=events,verbs=create;patch

func (r *SandboxReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)
	var sb sbv1.Sandbox
	if err := r.Get(ctx, req.NamespacedName, &sb); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !sb.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil // children go with it via owner references
	}
	before := sb.Status.DeepCopy()

	ttl := DefaultTTL
	if sb.Spec.TTL != nil {
		ttl = sb.Spec.TTL.Duration
	}
	expires := sb.CreationTimestamp.Add(ttl)
	if left := expires.Sub(r.Now()); left <= 0 {
		log.Info("ttl expired, deleting", "ttl", ttl)
		r.Recorder.Eventf(&sb, nil, corev1.EventTypeNormal, "Expired", "Delete", "ttl %s elapsed", ttl)
		sandboxesExpired.Inc()
		return ctrl.Result{}, client.IgnoreNotFound(r.Delete(ctx, &sb, client.PropagationPolicy(metav1.DeletePropagationBackground)))
	}
	sb.Status.ExpiresAt = &metav1.Time{Time: expires}

	result, err := r.converge(ctx, &sb)
	if err != nil {
		return ctrl.Result{}, err
	}
	sb.Status.ObservedGeneration = sb.Generation
	r.setReadyCondition(&sb)
	if sb.Status.Phase == sbv1.SandboxRunning && before.Phase != sbv1.SandboxRunning {
		sb.Status.ReadyAt = &metav1.Time{Time: r.Now()}
		sandboxReady.WithLabelValues(fmt.Sprint(sb.Spec.SnapshotRef != nil)).Observe(r.Now().Sub(sb.CreationTimestamp.Time).Seconds())
		r.Recorder.Eventf(&sb, nil, corev1.EventTypeNormal, "Running", "Start", "pod %s is running", sb.Status.PodName)
	}
	if sb.Status.Phase == sbv1.SandboxFailed && before.Phase != sbv1.SandboxFailed {
		r.Recorder.Eventf(&sb, nil, corev1.EventTypeWarning, "Failed", "Start", "%s", sb.Status.Message)
	}
	if !statusEqual(before, &sb.Status) {
		if err := r.Status().Update(ctx, &sb); err != nil {
			return ctrl.Result{}, err // conflicts are retried with backoff
		}
	}
	// wake up again for expiry even if nothing else changes
	left := expires.Sub(r.Now())
	if result.RequeueAfter == 0 || left < result.RequeueAfter {
		result.RequeueAfter = left
	}
	return result, nil
}

// converge creates missing children and derives status. Children are never updated in place: a sandbox's
// spec is immutable (CRD validation), so anything that exists already has the right shape.
func (r *SandboxReconciler) converge(ctx context.Context, sb *sbv1.Sandbox) (ctrl.Result, error) {
	if sb.Status.Phase == sbv1.SandboxFailed {
		return ctrl.Result{}, nil // terminal: the client creates a new sandbox
	}
	template, sources, wait, err := r.resolve(ctx, sb)
	if err != nil {
		return ctrl.Result{}, err
	}
	if wait != "" {
		sb.Status.Phase, sb.Status.Message = phaseFor(wait), wait
		return ctrl.Result{}, nil // the snapshot watch wakes us up
	}
	sb.Status.Template = template

	for _, c := range template {
		if c.Volume == nil {
			continue
		}
		if err := r.ensure(ctx, sb, buildPVC(sb, c, sources[c.Name])); err != nil {
			return ctrl.Result{}, err
		}
	}
	if sb.Spec.Network != sbv1.NetworkCluster {
		if err := r.ensure(ctx, sb, buildNetworkPolicy(sb)); err != nil {
			return ctrl.Result{}, err
		}
	}
	desired := buildPod(sb, template)
	if err := r.ensure(ctx, sb, desired); err != nil {
		return ctrl.Result{}, err
	}
	var pod corev1.Pod
	if err := r.Get(ctx, client.ObjectKeyFromObject(desired), &pod); err != nil {
		return ctrl.Result{}, err
	}
	sb.Status.PodName = pod.Name
	sb.Status.Phase, sb.Status.Message = podState(&pod)
	return ctrl.Result{}, nil
}

// failure markers returned in `wait` that are terminal rather than "try again later"
const failPrefix = "failed: "

func phaseFor(wait string) sbv1.SandboxPhase {
	if len(wait) >= len(failPrefix) && wait[:len(failPrefix)] == failPrefix {
		return sbv1.SandboxFailed
	}
	return sbv1.SandboxPending
}

// resolve returns the containers to run and, per container, the VolumeSnapshot to clone its volume from.
// A non-empty `wait` means the sandbox can't start yet (or ever, if it starts with failPrefix).
func (r *SandboxReconciler) resolve(ctx context.Context, sb *sbv1.Sandbox) (template []sbv1.Container, sources map[string]string, wait string, err error) {
	if sb.Spec.SnapshotRef == nil {
		return sb.Spec.Containers, nil, "", nil
	}
	var snap sbv1.SandboxSnapshot
	err = r.Get(ctx, client.ObjectKey{Namespace: sb.Namespace, Name: sb.Spec.SnapshotRef.Name}, &snap)
	switch {
	case apierrors.IsNotFound(err):
		return nil, nil, failPrefix + "snapshot " + sb.Spec.SnapshotRef.Name + " not found", nil
	case err != nil:
		return nil, nil, "", err
	}
	switch snap.Status.Phase {
	case sbv1.SnapshotReady:
	case sbv1.SnapshotFailed:
		return nil, nil, failPrefix + "snapshot " + snap.Name + " failed: " + snap.Status.Message, nil
	default:
		return nil, nil, "waiting for snapshot " + snap.Name, nil
	}
	sources = map[string]string{}
	for _, v := range snap.Status.Volumes {
		sources[v.Container] = v.VolumeSnapshot
	}
	return snap.Status.Template, sources, "", nil
}

// ensure creates obj (owned by the sandbox) unless it already exists.
func (r *SandboxReconciler) ensure(ctx context.Context, sb *sbv1.Sandbox, obj client.Object) error {
	if err := controllerutil.SetControllerReference(sb, obj, r.Scheme); err != nil {
		return err
	}
	if err := r.Create(ctx, obj); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("create %T %s: %w", obj, obj.GetName(), err)
	}
	return nil
}

func (r *SandboxReconciler) setReadyCondition(sb *sbv1.Sandbox) {
	cond := metav1.Condition{Type: condReady, ObservedGeneration: sb.Generation, Message: sb.Status.Message}
	switch sb.Status.Phase {
	case sbv1.SandboxRunning:
		cond.Status, cond.Reason = metav1.ConditionTrue, "Running"
	case sbv1.SandboxFailed:
		cond.Status, cond.Reason = metav1.ConditionFalse, "Failed"
	default:
		cond.Status, cond.Reason = metav1.ConditionFalse, "Pending"
	}
	meta.SetStatusCondition(&sb.Status.Conditions, cond)
}

func (r *SandboxReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// restores wait on their snapshot: index sandboxes by snapshotRef so a snapshot change finds them
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &sbv1.Sandbox{}, indexSnapshotRef,
		func(o client.Object) []string {
			if ref := o.(*sbv1.Sandbox).Spec.SnapshotRef; ref != nil {
				return []string{ref.Name}
			}
			return nil
		}); err != nil {
		return err
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&sbv1.Sandbox{}).
		Owns(&corev1.Pod{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&networkingv1.NetworkPolicy{}).
		Watches(&sbv1.SandboxSnapshot{}, handler.EnqueueRequestsFromMapFunc(r.restoresOf)).
		Named("sandbox").
		Complete(r)
}

func (r *SandboxReconciler) restoresOf(ctx context.Context, o client.Object) []reconcile.Request {
	var list sbv1.SandboxList
	if err := r.List(ctx, &list, client.InNamespace(o.GetNamespace()), client.MatchingFields{indexSnapshotRef: o.GetName()}); err != nil {
		return nil
	}
	reqs := make([]reconcile.Request, 0, len(list.Items))
	for _, sb := range list.Items {
		reqs = append(reqs, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&sb)})
	}
	return reqs
}
