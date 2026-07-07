import { useState } from "react";

export const EXAMPLE_JSON = `[
  {
    "id": "task_001",
    "question": "阅读 data/app.log,统计 ERROR 行数,并把结果写入 report.md。",
    "rubric": "report.md 必须存在,且给出的 ERROR 行数为 3。",
    "files": { "data/app.log": "INFO boot ok\\nERROR db timeout\\nERROR retry failed\\nERROR gave up\\n" },
    "task_type": "log-analysis"
  },
  {
    "id": "task_002",
    "question": "计算 12 和 30 的最大公约数,把数字写入 answer.txt。",
    "rubric": "answer.txt 的内容是 6。"
  }
]`;

const FIELDS: { name: string; required: string; desc: string }[] = [
  { name: "id", required: "必填", desc: "唯一且文件系统安全(不能包含 / \\ ..),将用作任务工作目录名,建议 task_001 风格" },
  { name: "question", required: "必填", desc: "给被评估 agent 的任务文本(agent 看得到技能与该文本,看不到 rubric)" },
  { name: "rubric", required: "必填", desc: "LLM 判分的验收标准,必须客观可判定(如具体数值、文件名、必须包含的内容),避免“质量好”类模糊描述" },
  { name: "files", required: "可选", desc: "{相对路径: 文本内容},会预置到 agent 的工作目录(任务需要输入数据时用它内联提供)" },
  { name: "task_type", required: "可选", desc: "分组键,用于结果按类型汇总,默认 default" },
];

/** Collapsible JSON schema documentation for skilleval task files. */
export default function TaskSetFormatDoc() {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const copyExample = async () => {
    try {
      await navigator.clipboard.writeText(EXAMPLE_JSON);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable (e.g. http origin) — button just stays put */
    }
  };

  return (
    <div className="rounded border border-line bg-panel2/40" data-testid="format-doc">
      <button
        type="button"
        className="w-full px-3 py-2 text-left text-sm text-cyan hover:text-text flex items-center gap-2"
        onClick={() => setOpen((v) => !v)}
        data-testid="format-doc-toggle"
      >
        <span className="text-xs">{open ? "▾" : "▸"}</span>
        查看 JSON 格式说明
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3" data-testid="format-doc-body">
          <p className="text-xs text-muted">
            任务文件是一个 JSON 数组(上传时也接受每行一个对象的 JSONL),每个元素是一个任务。
            split 模式则是 train / val / test 三个相同 schema 的文件。
          </p>
          <table className="w-full text-xs">
            <thead>
              <tr>
                <th className="th !py-1">字段</th>
                <th className="th !py-1">必填</th>
                <th className="th !py-1">说明</th>
              </tr>
            </thead>
            <tbody>
              {FIELDS.map((field) => (
                <tr key={field.name}>
                  <td className="td !py-1 font-mono text-green">{field.name}</td>
                  <td className="td !py-1">{field.required}</td>
                  <td className="td !py-1 text-muted">{field.desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="relative">
            <pre className="rounded bg-bg/80 border border-line p-3 text-[11px] leading-relaxed overflow-x-auto font-mono text-text/90">
              {EXAMPLE_JSON}
            </pre>
            <button
              type="button"
              className="btn-ghost absolute top-2 right-2 !px-2 !py-1 text-xs"
              onClick={copyExample}
              data-testid="format-doc-copy"
            >
              {copied ? "已复制 ✓" : "复制示例"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
