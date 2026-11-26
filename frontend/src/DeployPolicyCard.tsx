import { useEffect, useState, type FormEvent } from 'react'
import {
  api,
  type DeployPolicyEvaluation,
  type DeployPolicySummary,
  type DeviceGroupSummary,
  type PolicyMatchMode,
} from './api'
import {
  LOAD_IDLE,
  LOAD_PENDING,
  LOAD_READY,
  loadErrorMessage,
  loadFailed,
  resolveLoadError,
  type LoadState,
} from './loadState'

export function DeployPolicyCard({
  projectSlug,
  active,
  groups,
  onDeployed,
  onSessionExpired,
}: {
  projectSlug: string
  active: boolean
  groups: DeviceGroupSummary[]
  onDeployed: () => void
  onSessionExpired: () => void
}) {
  const [loadState, setLoadState] = useState<LoadState>(LOAD_IDLE)
  const [reloadSeq, setReloadSeq] = useState(0)
  const [policies, setPolicies] = useState<DeployPolicySummary[]>([])
  const [name, setName] = useState('')
  const [matchMode, setMatchMode] = useState<PolicyMatchMode>('tag')
  const [matchRef, setMatchRef] = useState('v*')
  const [selectedRings, setSelectedRings] = useState<string[]>([])
  const [maxParallel, setMaxParallel] = useState('5')
  const [minSoakS, setMinSoakS] = useState('0')
  const [minFreeHeap, setMinFreeHeap] = useState('')
  const [requireSim, setRequireSim] = useState(false)
  const [evalVersionByPolicy, setEvalVersionByPolicy] = useState<Record<string, string>>({})
  const [evalResultByPolicy, setEvalResultByPolicy] = useState<
    Record<string, DeployPolicyEvaluation>
  >({})
  const [pendingEval, setPendingEval] = useState<{ policyId: string; version: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [statusMsg, setStatusMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  const groupNameById = new Map(groups.map((g) => [g.id, g.name]))

  useEffect(() => {
    setPolicies([])
    setPendingEval(null)
    setEvalResultByPolicy({})
    setStatusMsg(null)
    setErrorMsg(null)
    setLoadState(LOAD_IDLE)
  }, [projectSlug])

  useEffect(() => {
    if (!active) return
    let cancelled = false
    setLoadState(LOAD_PENDING)
    setErrorMsg(null)
    api
      .listPolicies(projectSlug)
      .then((res) => {
        if (cancelled) return
        setPolicies(res.policies ?? [])
        setLoadState(LOAD_READY)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const msg = resolveLoadError(err, 'Could not load deploy policies', onSessionExpired)
        if (msg !== null) {
          setLoadState(loadFailed(msg))
        }
      })
    return () => {
      cancelled = true
    }
  }, [active, projectSlug, onSessionExpired, reloadSeq])

  function addRing(groupId: string) {
    if (!groupId || selectedRings.includes(groupId)) return
    setSelectedRings((prev) => [...prev, groupId])
  }

  function removeRing(groupId: string) {
    setSelectedRings((prev) => prev.filter((id) => id !== groupId))
  }

  async function handleCreate(e: FormEvent) {
    e.preventDefault()
    if (!name.trim() || !matchRef.trim() || selectedRings.length === 0) return
    setBusy(true)
    setStatusMsg(null)
    setErrorMsg(null)
    try {
      const created = await api.createPolicy(projectSlug, {
        name: name.trim(),
        match_mode: matchMode,
        match_ref: matchRef.trim(),
        ring_group_ids: selectedRings,
        max_parallel: Math.max(1, Number.parseInt(maxParallel, 10) || 5),
        min_soak_s: Math.max(0, Number.parseInt(minSoakS, 10) || 0),
        require_sim_passed: requireSim,
        ...(minFreeHeap.trim() !== ''
          ? { min_free_heap_bytes: Math.max(0, Number.parseInt(minFreeHeap, 10) || 0) }
          : {}),
      })
      setPolicies((prev) => [...prev, created])
      setName('')
      setSelectedRings([])
      setStatusMsg(`Created multi-ring policy "${created.name}".`)
    } catch (err) {
      const msg = resolveLoadError(err, 'Failed to create deploy policy', onSessionExpired)
      if (msg !== null) {
        setErrorMsg(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  async function handleToggle(policy: DeployPolicySummary) {
    setBusy(true)
    setErrorMsg(null)
    try {
      const updated = await api.updatePolicy(projectSlug, policy.id, {
        enabled: !policy.enabled,
      })
      setPolicies((prev) => prev.map((p) => (p.id === policy.id ? updated : p)))
    } catch (err) {
      const msg = resolveLoadError(err, 'Failed to update deploy policy', onSessionExpired)
      if (msg !== null) {
        setErrorMsg(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete(policyId: string) {
    setBusy(true)
    setErrorMsg(null)
    try {
      await api.deletePolicy(projectSlug, policyId)
      setPolicies((prev) => prev.filter((p) => p.id !== policyId))
      setPendingEval((prev) => (prev?.policyId === policyId ? null : prev))
      setStatusMsg('Deleted deploy policy.')
    } catch (err) {
      const msg = resolveLoadError(err, 'Failed to delete deploy policy', onSessionExpired)
      if (msg !== null) {
        setErrorMsg(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  async function handleEvaluate(policyId: string, version: string) {
    const trimmed = version.trim()
    if (!trimmed) return
    setBusy(true)
    setErrorMsg(null)
    try {
      const result = await api.evaluatePolicy(projectSlug, policyId, trimmed)
      setEvalResultByPolicy((prev) => ({ ...prev, [policyId]: result }))
      setPendingEval(null)
      if (result.deployed.length > 0) {
        onDeployed()
      }
    } catch (err) {
      const msg = resolveLoadError(err, 'Policy evaluation failed', onSessionExpired)
      if (msg !== null) {
        setErrorMsg(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  const displayError = errorMsg ?? loadErrorMessage(loadState)

  return (
    <section
      className="console-card"
      aria-labelledby="deploy-policy-heading"
      data-testid="deploy-policy-card"
    >
      <div className="section-header">
        <h2 id="deploy-policy-heading">Staged cohort deploy policies (R11)</h2>
      </div>
      <p className="muted">
        Promote matching Git branches or tags through ordered device-group rings (e.g.{' '}
        <code>canary → staging → prod</code>). Any rollback, failure, safe-mode entry, fault reset,
        or free-heap breach automatically halts promotion to the next cohort wave.
      </p>
      <form className="compact-form" onSubmit={(e) => void handleCreate(e)}>
        <div className="form-grid form-grid-3">
          <label>
            Policy name{' '}
            <input
              type="text"
              aria-label="Policy name"
              placeholder="prod-release-rings"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Match mode{' '}
            <select
              aria-label="Policy match mode"
              value={matchMode}
              onChange={(e) => setMatchMode(e.target.value as PolicyMatchMode)}
              disabled={busy}
            >
              <option value="tag">tag glob (e.g. v*)</option>
              <option value="branch">branch (e.g. main)</option>
            </select>
          </label>
          <label>
            Match ref{' '}
            <input
              type="text"
              aria-label="Policy match ref"
              placeholder="v*"
              value={matchRef}
              onChange={(e) => setMatchRef(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        <div className="form-grid form-grid-3">
          <label>
            Add cohort ring{' '}
            <select
              aria-label="Add cohort ring"
              value=""
              onChange={(e) => addRing(e.target.value)}
              disabled={busy || groups.length === 0}
            >
              <option value="">
                {groups.length === 0 ? '(create groups in Access first)' : '+ select group ring…'}
              </option>
              {groups
                .filter((g) => !selectedRings.includes(g.id))
                .map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Min soak (s){' '}
            <input
              type="number"
              aria-label="Policy min soak seconds"
              min={0}
              value={minSoakS}
              onChange={(e) => setMinSoakS(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Min free heap (bytes, optional){' '}
            <input
              type="number"
              aria-label="Policy min free heap"
              min={0}
              placeholder="65536"
              value={minFreeHeap}
              onChange={(e) => setMinFreeHeap(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        {selectedRings.length > 0 && (
          <p data-testid="selected-policy-rings">
            <strong>Ordered rings:</strong>{' '}
            {selectedRings.map((gid, idx) => (
              <span key={gid}>
                {idx > 0 ? ' → ' : ''}
                <code>{groupNameById.get(gid) ?? gid.slice(0, 8)}</code>{' '}
                <button
                  type="button"
                  onClick={() => removeRing(gid)}
                  aria-label={`Remove ring ${idx + 1}`}
                >
                  ×
                </button>
              </span>
            ))}
          </p>
        )}
        <p>
          <label className="choice">
            <input
              type="checkbox"
              checked={requireSim}
              onChange={(e) => setRequireSim(e.target.checked)}
              disabled={busy}
            />{' '}
            Require simulation passed (<code>sim_status = passed</code>)
          </label>{' '}
          <label style={{ display: 'inline-block', marginLeft: '1rem' }}>
            Max parallel{' '}
            <input
              type="number"
              aria-label="Policy max parallel"
              min={1}
              max={100}
              style={{ width: '4.5rem' }}
              value={maxParallel}
              onChange={(e) => setMaxParallel(e.target.value)}
              disabled={busy}
            />
          </label>{' '}
          <button
            type="submit"
            data-testid="create-policy-btn"
            disabled={busy || !name.trim() || !matchRef.trim() || selectedRings.length === 0}
          >
            Create policy
          </button>
        </p>
      </form>
      {statusMsg !== null && (
        <p className="ok" data-testid="policy-status">
          {statusMsg}
        </p>
      )}
      {displayError !== null && (
        <p className="bad" role="alert" data-testid="policy-error">
          {displayError}
          {loadState.status === 'error' && (
            <>
              {' '}
              <button
                type="button"
                data-testid="policy-retry-btn"
                onClick={() => setReloadSeq((n) => n + 1)}
              >
                Retry
              </button>
            </>
          )}
        </p>
      )}
      {loadState.status === 'loading' && policies.length === 0 && (
        <p className="muted" aria-busy="true" data-testid="policy-loading">
          Loading deploy policies…
        </p>
      )}
      {loadState.status === 'loaded' && policies.length === 0 && (
        <p className="muted" data-testid="policy-empty">
          No deploy policies configured yet.
        </p>
      )}
      {policies.length > 0 && (
        <table data-testid="deploy-policies-table">
          <thead>
            <tr>
              <th scope="col">Policy</th>
              <th scope="col">Trigger</th>
              <th scope="col">Cohort rings</th>
              <th scope="col">Gates</th>
              <th scope="col">Evaluate / Controls</th>
            </tr>
          </thead>
          <tbody>
            {policies.map((p) => {
              const ev = evalResultByPolicy[p.id]
              const draftVersion = (evalVersionByPolicy[p.id] ?? '').trim()
              const isConfirmingEval = pendingEval?.policyId === p.id
              return (
                <tr key={p.id} data-testid="deploy-policy-row">
                  <td>
                    <strong>{p.name}</strong>{' '}
                    <span className={p.enabled ? 'ok' : 'muted'}>
                      ({p.enabled ? 'enabled' : 'paused'})
                    </span>
                  </td>
                  <td>
                    <code>
                      {p.match_mode}:{p.match_ref}
                    </code>
                  </td>
                  <td>
                    {p.ring_group_ids
                      .map((gid) => groupNameById.get(gid) ?? gid.slice(0, 8))
                      .join(' → ')}
                  </td>
                  <td>
                    <code>max={p.max_parallel}</code>
                    {p.min_soak_s > 0 && <code> soak={p.min_soak_s}s</code>}
                    {p.require_sim_passed && <code> sim=passed</code>}
                    {p.min_free_heap_bytes !== null && (
                      <code> heap&gt;={p.min_free_heap_bytes}B</code>
                    )}
                  </td>
                  <td>
                    <input
                      type="text"
                      aria-label={`Evaluate version for ${p.name}`}
                      placeholder="version"
                      style={{ width: '6rem' }}
                      value={evalVersionByPolicy[p.id] ?? ''}
                      onChange={(e) => {
                        const nextVal = e.target.value
                        setEvalVersionByPolicy((prev) => ({ ...prev, [p.id]: nextVal }))
                        setPendingEval((prev) => (prev?.policyId === p.id ? null : prev))
                      }}
                    />{' '}
                    {isConfirmingEval ? (
                      <span data-testid="confirm-evaluate-policy">
                        <button
                          type="button"
                          className="bad"
                          data-testid="confirm-evaluate-policy-btn"
                          disabled={busy || !pendingEval.version}
                          onClick={() => void handleEvaluate(p.id, pendingEval.version)}
                        >
                          Confirm evaluate
                        </button>{' '}
                        <button
                          type="button"
                          data-testid="cancel-evaluate-policy-btn"
                          disabled={busy}
                          onClick={() => setPendingEval(null)}
                        >
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        data-testid="evaluate-policy-btn"
                        disabled={busy || !draftVersion}
                        onClick={() => {
                          if (!draftVersion) return
                          setPendingEval({ policyId: p.id, version: draftVersion })
                        }}
                      >
                        Evaluate
                      </button>
                    )}{' '}
                    <button
                      type="button"
                      data-testid="toggle-policy-btn"
                      disabled={busy}
                      onClick={() => void handleToggle(p)}
                    >
                      {p.enabled ? 'Pause' : 'Enable'}
                    </button>{' '}
                    <button
                      type="button"
                      data-testid="delete-policy-btn"
                      disabled={busy}
                      onClick={() => void handleDelete(p.id)}
                    >
                      Delete
                    </button>
                    {ev && (
                      <div
                        className={
                          ev.status === 'halted'
                            ? 'bad'
                            : ev.status === 'completed'
                              ? 'ok'
                              : 'muted'
                        }
                        data-testid="policy-eval-summary"
                      >
                        <strong>{ev.status}</strong>
                        {ev.halt_reason ? ` — ${ev.halt_reason}` : ''} (
                        {ev.rings
                          .map(
                            (r) =>
                              `ring ${r.ring_index}: ${r.status} ${r.confirmed_devices}/${r.total_devices}`,
                          )
                          .join(', ')}
                        )
                      </div>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
