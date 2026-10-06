# Forge benchmarks

_Generated 2026-10-07 13:24 on arm64, 12 CPUs. Also served at `/api/bench`._

## Pipeline (mine → validate → rollout → QA)

```json
{
  "tasks": {
    "total": 88,
    "valid": 54,
    "rejected": 34,
    "reject_reasons": {
      "no fail-to-pass tests": 27,
      "no test results after fix": 2,
      "flaky": 5
    }
  },
  "mutation_score": {
    "tasks": 45,
    "mean": 0.807,
    "weak_graders(<0.5)": 3
  },
  "rollouts": {
    "agent_root": {
      "n": 28,
      "robust_pass": 3,
      "naive_pass": 3,
      "verdicts": {
        "clean": 24,
        "suspect": 4,
        "hack": 0
      },
      "mean_steps": 10.9
    },
    "agent_forks": {
      "n": 4,
      "robust_pass": 0,
      "naive_pass": 0,
      "verdicts": {
        "clean": 3,
        "suspect": 1,
        "hack": 0
      },
      "mean_steps": 13
    },
    "redteam": {
      "n": 5,
      "robust_pass": 0,
      "naive_pass": 0,
      "verdicts": {
        "clean": 4,
        "suspect": 0,
        "hack": 1
      },
      "mean_steps": 6.8
    }
  },
  "step_snapshot_ms": {
    "n": 360,
    "p50": 956.2,
    "p95": 1899.7
  }
}
```

## Reward-hacking corpus: grader robustness + detector

```json
{
  "samples": 378,
  "tasks": 54,
  "naive_false_pass_rate": 1.0,
  "robust_false_pass_rate": 0.0,
  "gold_robust_pass_rate": 1.0,
  "detector_recall": 1.0,
  "detector_static_recall": 1.0,
  "detector_precision": 1.0,
  "false_positive_rate": 0.0,
  "by_kind": {
    "gold": {
      "n": 54,
      "naive_pass": 53,
      "robust_pass": 54,
      "flagged": 0,
      "static_flagged": 0
    },
    "noop": {
      "n": 54,
      "naive_pass": 0,
      "robust_pass": 0,
      "flagged": 0,
      "static_flagged": 0
    },
    "conftest_force_pass": {
      "n": 54,
      "naive_pass": 54,
      "robust_pass": 0,
      "flagged": 54,
      "static_flagged": 54
    },
    "conftest_skip_all": {
      "n": 54,
      "naive_pass": 54,
      "robust_pass": 0,
      "flagged": 54,
      "static_flagged": 54
    },
    "exit_zero_on_import": {
      "n": 54,
      "naive_pass": 54,
      "robust_pass": 0,
      "flagged": 54,
      "static_flagged": 54
    },
    "pytest_ini_collect_only": {
      "n": 54,
      "naive_pass": 54,
      "robust_pass": 0,
      "flagged": 54,
      "static_flagged": 54
    },
    "site_packages_tamper": {
      "n": 54,
      "naive_pass": 54,
      "robust_pass": 0,
      "flagged": 54,
      "static_flagged": 54
    }
  }
}
```

## Queue throughput: no-op jobs over NATS + Postgres

```json
{
  "jobs": 20000,
  "workers": 4,
  "slots": 128,
  "done": 20000,
  "elapsed_s": 8.6,
  "jobs_per_s": 2335.6
}
```

## Chaos: SIGKILL workers, transactional effects

```json
{
  "mode": "tx",
  "jobs": 2000,
  "workers": 6,
  "sigkills": 21,
  "elapsed_s": 8.6,
  "jobs_done": 2000,
  "jobs_retried_after_crash": 233,
  "effects_written": 2000,
  "distinct_effects": 2000,
  "duplicate_effects": 0,
  "lost": 0,
  "throughput_jobs_s": 233.9
}
```

## Chaos: SIGKILL workers, naive effects (baseline)

```json
{
  "mode": "naive",
  "jobs": 2000,
  "workers": 6,
  "sigkills": 23,
  "elapsed_s": 9.4,
  "jobs_done": 2000,
  "jobs_retried_after_crash": 274,
  "effects_written": 2031,
  "distinct_effects": 2000,
  "duplicate_effects": 31,
  "lost": 0,
  "throughput_jobs_s": 212.9
}
```

## 100k concurrent simulated agents, crash + resume

```json
{
  "agents": 100000,
  "steps_per_agent": 6,
  "inflight_limit": 20000,
  "phase1_crash": {
    "peak": 100000,
    "live": 0,
    "llm_calls": 213035,
    "writes": 213022,
    "crashed": true,
    "elapsed_s": 29.99
  },
  "durable_steps_at_crash": 213035,
  "phase2_resume": {
    "peak": 100000,
    "live": 0,
    "llm_calls": 386965,
    "writes": 386965,
    "crashed": false,
    "elapsed_s": 19.29
  },
  "steps_recorded": 600000,
  "expected_steps": 600000,
  "agents_complete": 100000,
  "exactly_once": true,
  "peak_rss_mb": 305.9
}
```

## Sandbox lifecycle / snapshot / restore latency

```json
{
  "sandboxes": 40,
  "parallel": 16,
  "wall_s": 20.2,
  "sandbox_lifecycles_per_s": 1.98,
  "start_ms": {
    "p50": 982.9,
    "p95": 1446.2
  },
  "exec_ms": {
    "p50": 304.2,
    "p95": 500.8
  },
  "snapshot_ms": {
    "p50": 3910.3,
    "p95": 4798.6
  },
  "restore_ms": {
    "p50": 1072.7,
    "p95": 1594.2
  }
}
```

## Multi-node env (app+postgres+redis) group snapshot & fork

```json
{
  "create": {
    "id": 1,
    "start_ms": 3686.3,
    "ready_ms": 2059.8
  },
  "snapshot": {
    "snapshot": 1,
    "state": {
      "pg_orders": 20,
      "pg_sum": 210,
      "redis_counter": 20
    },
    "quiesce_ms": 182.4,
    "frozen_ms": 2943.6,
    "total_ms": 3126.0
  },
  "original_after_more_writes": {
    "pg_orders": 35,
    "pg_sum": 330,
    "redis_counter": 35
  },
  "parallel_restore_ms": 3963.5,
  "fork_start": [
    {
      "id": 2,
      "start_ms": 3862.3,
      "ready_ms": 2442.7
    },
    {
      "id": 3,
      "start_ms": 3863.7,
      "ready_ms": 2499.7
    }
  ],
  "fork_a_state": {
    "pg_orders": 25,
    "pg_sum": 225,
    "redis_counter": 25
  },
  "fork_b_state": {
    "pg_orders": 20,
    "pg_sum": 210,
    "redis_counter": 20
  },
  "checks": {
    "restore_matches_snapshot": true,
    "forks_isolated": true,
    "original_unaffected": true,
    "pg_redis_consistent": true
  }
}
```
