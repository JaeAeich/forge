package controller

import (
	"context"
	"fmt"
	"time"

	snapv1 "github.com/kubernetes-csi/external-snapshotter/client/v8/apis/volumesnapshot/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/events"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	sbv1 "forge.dev/operator/api/v1alpha1"
)

const indexSandboxName = ".spec.sandboxName"

// SnapshotReconciler captures a Running sandbox: it records the sandbox's container template and takes one
// VolumeSnapshot per volume. The VolumeSnapshots are owned by the SandboxSnapshot, not the sandbox, so a
// snapshot outlives the sandbox it came from and any number of sandboxes can be restored from it.
//
// Phases: Pending (sandbox not running yet) -> Cut (every volume's point-in-time copy exists; the client
// may let the sandbox keep writing) -> Ready (every volume can be restored). Failed is terminal.
type SnapshotReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder events.EventRecorder
	Now      func() time.Time
}

// +kubebuilder:rbac:groups=sandbox.forge.dev,resources=sandboxsnapshots,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=sandbox.forge.dev,resources=sandboxsnapshots/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=sandbox.forge.dev,resources=sandboxsnapshots/finalizers,verbs=update
// +kubebuilder:rbac:groups=snapshot.storage.k8s.io,resources=volumesnapshots,verbs=get;list;watch;create;delete

func (r *SnapshotReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var snap sbv1.SandboxSnapshot
	if err := r.Get(ctx, req.NamespacedName, &snap); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !snap.DeletionTimestamp.IsZero() || snap.Status.Phase == sbv1.SnapshotReady || snap.Status.Phase == sbv1.SnapshotFailed {
		return ctrl.Result{}, nil
	}
	before := snap.Status.DeepCopy()
	if err := r.converge(ctx, &snap); err != nil {
		return ctrl.Result{}, err
	}
	now := r.Now()
	if snap.Status.Phase != before.Phase {
		switch snap.Status.Phase {
		case sbv1.SnapshotCut:
			snapshotCut.Observe(now.Sub(snap.CreationTimestamp.Time).Seconds())
		case sbv1.SnapshotReady:
			if snap.Status.CutAt == nil { // cut and ready observed in the same pass
				snap.Status.CutAt = &metav1.Time{Time: now}
				snapshotCut.Observe(now.Sub(snap.CreationTimestamp.Time).Seconds())
			}
			snapshotReady.Observe(now.Sub(snap.CreationTimestamp.Time).Seconds())
			r.Recorder.Eventf(&snap, nil, corev1.EventTypeNormal, "Ready", "Snapshot", "%d volume(s) ready", len(snap.Status.Volumes))
		case sbv1.SnapshotFailed:
			r.Recorder.Eventf(&snap, nil, corev1.EventTypeWarning, "Failed", "Snapshot", "%s", snap.Status.Message)
		}
	}
	if snap.Status.Phase == sbv1.SnapshotCut && snap.Status.CutAt == nil {
		snap.Status.CutAt = &metav1.Time{Time: now}
	}
	if snap.Status.Phase == sbv1.SnapshotReady && snap.Status.ReadyAt == nil {
		snap.Status.ReadyAt = &metav1.Time{Time: now}
	}
	if !statusEqual(before, &snap.Status) {
		return ctrl.Result{}, r.Status().Update(ctx, &snap)
	}
	return ctrl.Result{}, nil
}

func (r *SnapshotReconciler) converge(ctx context.Context, snap *sbv1.SandboxSnapshot) error {
	if snap.Status.Template == nil { // not captured yet
		var sb sbv1.Sandbox
		err := r.Get(ctx, client.ObjectKey{Namespace: snap.Namespace, Name: snap.Spec.SandboxName}, &sb)
		switch {
		case apierrors.IsNotFound(err):
			snap.Status.Phase, snap.Status.Message = sbv1.SnapshotFailed, "sandbox "+snap.Spec.SandboxName+" not found"
			return nil
		case err != nil:
			return err
		}
		switch sb.Status.Phase {
		case sbv1.SandboxRunning:
		case sbv1.SandboxFailed:
			snap.Status.Phase, snap.Status.Message = sbv1.SnapshotFailed, "sandbox failed: "+sb.Status.Message
			return nil
		default:
			snap.Status.Phase, snap.Status.Message = sbv1.SnapshotPending, "waiting for sandbox to run"
			return nil // the sandbox watch wakes us up
		}
		snap.Status.Template = sb.Status.Template
		snap.Status.Volumes = []sbv1.VolumeSnapshotRef{}
		for _, c := range sb.Status.Template {
			if c.Volume != nil {
				snap.Status.Volumes = append(snap.Status.Volumes, sbv1.VolumeSnapshotRef{
					Container: c.Name, VolumeSnapshot: volumeSnapshotName(snap.Name, c.Name),
				})
			}
		}
		snap.Status.Phase, snap.Status.Message = sbv1.SnapshotPending, "cutting"
	}

	cut, ready := true, true
	for _, v := range snap.Status.Volumes {
		vs := buildVolumeSnapshot(snap, v.Container)
		if err := controllerutil.SetControllerReference(snap, vs, r.Scheme); err != nil {
			return err
		}
		if err := r.Create(ctx, vs); err != nil && !apierrors.IsAlreadyExists(err) {
			return fmt.Errorf("create VolumeSnapshot %s: %w", vs.Name, err)
		}
		if err := r.Get(ctx, client.ObjectKeyFromObject(vs), vs); err != nil {
			if apierrors.IsNotFound(err) { // just created; the cache will catch up and wake us
				cut, ready = false, false
				continue
			}
			return err
		}
		st := vs.Status
		if st != nil && st.Error != nil && st.Error.Message != nil {
			snap.Status.Phase, snap.Status.Message = sbv1.SnapshotFailed, v.Container+": "+*st.Error.Message
			return nil
		}
		cut = cut && st != nil && st.CreationTime != nil
		ready = ready && st != nil && st.ReadyToUse != nil && *st.ReadyToUse
	}
	switch {
	case ready:
		snap.Status.Phase, snap.Status.Message = sbv1.SnapshotReady, ""
	case cut:
		snap.Status.Phase, snap.Status.Message = sbv1.SnapshotCut, "waiting for volumes to be ready"
	}
	return nil
}

func (r *SnapshotReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &sbv1.SandboxSnapshot{}, indexSandboxName,
		func(o client.Object) []string { return []string{o.(*sbv1.SandboxSnapshot).Spec.SandboxName} }); err != nil {
		return err
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&sbv1.SandboxSnapshot{}).
		Owns(&snapv1.VolumeSnapshot{}).
		Watches(&sbv1.Sandbox{}, handler.EnqueueRequestsFromMapFunc(r.snapshotsOf)).
		Named("sandboxsnapshot").
		Complete(r)
}

func (r *SnapshotReconciler) snapshotsOf(ctx context.Context, o client.Object) []reconcile.Request {
	var list sbv1.SandboxSnapshotList
	if err := r.List(ctx, &list, client.InNamespace(o.GetNamespace()), client.MatchingFields{indexSandboxName: o.GetName()}); err != nil {
		return nil
	}
	var reqs []reconcile.Request
	for _, s := range list.Items {
		if s.Status.Phase == "" || s.Status.Phase == sbv1.SnapshotPending {
			reqs = append(reqs, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&s)})
		}
	}
	return reqs
}
