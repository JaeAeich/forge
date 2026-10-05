package controller

import (
	"github.com/prometheus/client_golang/prometheus"
	"k8s.io/apimachinery/pkg/api/equality"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

var (
	sandboxReady = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "forge_sandbox_ready_seconds",
		Help:    "Time from Sandbox creation to all containers running.",
		Buckets: prometheus.ExponentialBuckets(0.5, 2, 10),
	}, []string{"restored"})
	snapshotCut = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name:    "forge_sandbox_snapshot_cut_seconds",
		Help:    "Time from SandboxSnapshot creation until every volume's point-in-time copy is taken.",
		Buckets: prometheus.ExponentialBuckets(0.1, 2, 12),
	})
	snapshotReady = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name:    "forge_sandbox_snapshot_ready_seconds",
		Help:    "Time from SandboxSnapshot creation until every volume is restorable.",
		Buckets: prometheus.ExponentialBuckets(0.1, 2, 14),
	})
	sandboxesExpired = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "forge_sandbox_expired_total",
		Help: "Sandboxes deleted because their TTL elapsed (usually a client that died).",
	})
)

func init() {
	metrics.Registry.MustRegister(sandboxReady, snapshotCut, snapshotReady, sandboxesExpired)
}

func statusEqual(a, b any) bool { return equality.Semantic.DeepEqual(a, b) }
