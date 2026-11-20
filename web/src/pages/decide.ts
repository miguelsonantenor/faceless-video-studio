// Recording the creator's decision (REV-3). A failed check can be approved only with a
// note saying why it is acceptable (QA-4); a rejection always needs a note.
import { api } from "../api";

export type Decision = "approve" | "reject" | "skip" | "reopen";

interface Result {
  items: { project?: unknown; error?: { message: string; fix: string } }[];
  summary: string;
}

export async function decide(project: number, decision: Decision,
  opts: { note?: string; overrideQa?: boolean } = {}): Promise<string> {
  const result = await api<Result>("/api/review", {
    method: "POST",
    body: { projects: [project], decision, note: opts.note || null,
      override_qa: !!opts.overrideQa },
  });
  return result.summary;
}
