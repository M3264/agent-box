/**
 * The artifacts tab.
 *
 * The rows come from the per-job artifacts query in the job snapshot. v1's screen
 * read `j.artifacts` off the `/api/runs` list rows — a key that endpoint never
 * returned — so it was permanently empty even when artifacts existed.
 */

import { downloadUrl } from '../../api'
import { Empty } from '../../components/ui'
import { bytes, fullTime } from '../../lib/format'
import type { Artifact, Phase } from '../../types'

interface Props {
  jobId: string
  artifacts: Artifact[]
  plan: Phase[]
}

export function ArtifactsView({ jobId, artifacts, plan }: Props) {
  if (artifacts.length === 0) {
    return <Empty title="No artifacts yet" hint="Phase outputs and the final result are written here." />
  }

  const phaseName = (id: number | null): string => {
    if (id === null) return '—'
    const phase = plan.find((candidate) => candidate.id === id)
    return phase ? `${phase.seq}. ${phase.name}` : '—'
  }

  return (
    <table className="table artifacts">
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Phase</th>
          <th scope="col">By</th>
          <th scope="col">Type</th>
          <th scope="col">Size</th>
          <th scope="col">Created</th>
        </tr>
      </thead>
      <tbody>
        {artifacts.map((artifact) => (
          <tr key={artifact.id}>
            <th scope="row">
              <a
                href={downloadUrl(jobId, artifact.id)}
                target="_blank"
                rel="noreferrer"
                className="link"
              >
                {artifact.name}
              </a>
            </th>
            <td>{phaseName(artifact.phase_id)}</td>
            <td>{artifact.agent ?? '—'}</td>
            <td className="mono">{artifact.mime_type}</td>
            <td className="num">{bytes(artifact.size)}</td>
            <td>{fullTime(artifact.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
