import { TaskItem } from "../api";

const UNSAFE_ID = /[/\\]|\.\./;

/** Suggest the next free task_00N-style id. */
export function nextTaskId(items: TaskItem[]): string {
  const used = new Set(items.map((item) => item.id));
  for (let index = items.length + 1; ; index++) {
    const candidate = `task_${String(index).padStart(3, "0")}`;
    if (!used.has(candidate)) return candidate;
  }
}

export function emptyItem(items: TaskItem[]): TaskItem {
  return { id: nextTaskId(items), question: "", rubric: "" };
}

/** Per-row Chinese validation errors, keyed by row index. */
export function rowErrors(items: TaskItem[]): Map<number, string[]> {
  const errors = new Map<number, string[]>();
  const push = (index: number, message: string) => {
    errors.set(index, [...(errors.get(index) ?? []), message]);
  };
  const firstIndexOfId = new Map<string, number>();
  items.forEach((item, index) => {
    const id = (item.id ?? "").trim();
    if (!id) push(index, "id 不能为空");
    else if (UNSAFE_ID.test(id)) push(index, "id 不能包含 /、\\ 或 ..(将用作目录名)");
    else if (firstIndexOfId.has(id)) push(index, `id "${id}" 与第 ${firstIndexOfId.get(id)! + 1} 行重复`);
    else firstIndexOfId.set(id, index);
    if (!(item.question ?? "").trim()) push(index, "question 不能为空");
    if (!(item.rubric ?? "").trim()) push(index, "rubric 不能为空");
  });
  return errors;
}

/** Flat error list for submit-time checks (empty array = valid). */
export function validateItems(items: TaskItem[]): string[] {
  if (items.length === 0) return ["至少需要 1 个任务"];
  const messages: string[] = [];
  for (const [index, rowMessages] of rowErrors(items)) {
    for (const message of rowMessages) messages.push(`第 ${index + 1} 行:${message}`);
  }
  return messages;
}

/**
 * Row-based task editor. Rows are shallow copies of the original task objects:
 * only the four editable fields are overwritten, so `files` and any ride-along
 * fields survive the round-trip untouched.
 */
export default function TaskItemsEditor({
  items,
  onChange,
}: {
  items: TaskItem[];
  onChange: (items: TaskItem[]) => void;
}) {
  const errors = rowErrors(items);

  const patchRow = (index: number, patch: Partial<TaskItem>) => {
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  };
  const removeRow = (index: number) => {
    onChange(items.filter((_, i) => i !== index));
  };

  return (
    <div className="space-y-3">
      {items.map((item, index) => {
        const rowErrs = errors.get(index) ?? [];
        const fileNames = Object.keys(item.files ?? {});
        return (
          <div
            key={index}
            className={`rounded border p-3 space-y-2 ${rowErrs.length > 0 ? "border-red/70" : "border-line"} bg-panel2/30`}
            data-testid="taskitem-row"
          >
            <div className="flex items-center gap-3">
              <span className="text-xs text-muted w-10 shrink-0">#{index + 1}</span>
              <input
                className="input !py-1 text-sm font-mono flex-1"
                placeholder="id(如 task_001)"
                value={item.id}
                onChange={(event) => patchRow(index, { id: event.target.value })}
                data-testid="taskitem-id"
              />
              <input
                className="input !py-1 text-sm font-mono w-40"
                placeholder="task_type(可选)"
                value={item.task_type ?? ""}
                onChange={(event) =>
                  patchRow(index, { task_type: event.target.value || undefined })
                }
                data-testid="taskitem-type"
              />
              {fileNames.length > 0 && (
                <span
                  className="font-mono text-[11px] text-amber shrink-0"
                  title={fileNames.join("\n")}
                >
                  附带 {fileNames.length} 个文件
                </span>
              )}
              <button
                type="button"
                className="btn-danger !px-2 !py-1 text-xs shrink-0"
                onClick={() => removeRow(index)}
                data-testid="taskitem-remove"
              >
                删除
              </button>
            </div>
            <textarea
              className="input text-sm min-h-[56px]"
              placeholder="question — 给被评估 agent 的任务文本"
              value={item.question}
              onChange={(event) => patchRow(index, { question: event.target.value })}
              data-testid="taskitem-question"
            />
            <textarea
              className="input text-sm min-h-[56px]"
              placeholder="rubric — 客观可判定的验收标准(判分依据)"
              value={item.rubric}
              onChange={(event) => patchRow(index, { rubric: event.target.value })}
              data-testid="taskitem-rubric"
            />
            {rowErrs.length > 0 && (
              <div className="text-xs text-red" data-testid="taskitem-errors">
                {rowErrs.join(";")}
              </div>
            )}
          </div>
        );
      })}
      <button
        type="button"
        className="btn-ghost text-sm"
        onClick={() => onChange([...items, emptyItem(items)])}
        data-testid="taskitem-add"
      >
        + 添加任务
      </button>
    </div>
  );
}
