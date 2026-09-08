export interface SkillDiscoveryCandidate {
  candidateId: string;
  status: string;
  name?: string;
  displayName?: string;
  path?: string;
  version?: string;
  [key: string]: unknown;
}

export interface SkillImportResult<T> {
  candidateId: string;
  status: "succeeded" | "failed";
  value?: T;
  error?: string;
}

export interface SkillImportSummary<T> {
  results: SkillImportResult<T>[];
  succeededIds: string[];
  failedIds: string[];
}

function isEligible(candidate: SkillDiscoveryCandidate): boolean {
  return candidate.status === "ready" || candidate.status === "conflict";
}

export function eligibleSkillIds(
  candidates: readonly SkillDiscoveryCandidate[],
): Set<string> {
  return new Set(candidates.filter(isEligible).map(candidate => candidate.candidateId));
}

export async function runSkillImportBatch<
  TValue,
  TCandidate extends SkillDiscoveryCandidate = SkillDiscoveryCandidate,
>({
  candidates,
  selectedIds,
  overwriteIds,
  commit,
  onResult,
}: {
  candidates: readonly TCandidate[];
  selectedIds: ReadonlySet<string>;
  overwriteIds: ReadonlySet<string>;
  commit: (candidate: TCandidate, overwrite: boolean) => Promise<TValue>;
  onResult?: (
    result: SkillImportResult<TValue>,
    completed: number,
    total: number,
  ) => void;
}): Promise<SkillImportSummary<TValue>> {
  const queue = candidates.filter(
    candidate => selectedIds.has(candidate.candidateId) && isEligible(candidate),
  );
  const results: SkillImportResult<TValue>[] = [];

  for (const candidate of queue) {
    let result: SkillImportResult<TValue>;
    try {
      const value = await commit(candidate, overwriteIds.has(candidate.candidateId));
      result = { candidateId: candidate.candidateId, status: "succeeded", value };
    } catch (error) {
      result = {
        candidateId: candidate.candidateId,
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
      };
    }
    results.push(result);
    onResult?.(result, results.length, queue.length);
  }

  return {
    results,
    succeededIds: results
      .filter(result => result.status === "succeeded")
      .map(result => result.candidateId),
    failedIds: results
      .filter(result => result.status === "failed")
      .map(result => result.candidateId),
  };
}
