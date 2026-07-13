import { useState } from "react";
import { useTranslation } from "react-i18next";

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

const FIELDS: { name: string; required: "yes" | "no"; descKey: string }[] = [
  { name: "id", required: "yes", descKey: "id" },
  { name: "question", required: "yes", descKey: "question" },
  { name: "rubric", required: "yes", descKey: "rubric" },
  { name: "files", required: "no", descKey: "files" },
  { name: "task_type", required: "no", descKey: "task_type" },
];

/** Collapsible JSON schema documentation for skilleval task files. */
export default function TaskSetFormatDoc() {
  const { t } = useTranslation("tasksets");
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
    <div className="border border-line bg-panel2/40" data-testid="format-doc">
      <button
        type="button"
        className="w-full px-3 py-2 text-left text-sm text-amber hover:text-text flex items-center gap-2"
        onClick={() => setOpen((v) => !v)}
        data-testid="format-doc-toggle"
      >
        <span className="text-xs">{open ? "▾" : "▸"}</span>
        {t("formatDoc.toggle")}
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3" data-testid="format-doc-body">
          <p className="text-xs text-muted">
            {t("formatDoc.intro")}
          </p>
          <table className="w-full text-xs">
            <thead>
              <tr>
                <th className="th !py-1">{t("formatDoc.table.field")}</th>
                <th className="th !py-1">{t("formatDoc.table.required")}</th>
                <th className="th !py-1">{t("formatDoc.table.desc")}</th>
              </tr>
            </thead>
            <tbody>
              {FIELDS.map((field) => (
                <tr key={field.name}>
                  <td className="td !py-1 font-mono text-s2">{field.name}</td>
                  <td className="td !py-1">{t(`formatDoc.required.${field.required}`)}</td>
                  <td className="td !py-1 text-muted">{t(`formatDoc.fields.${field.descKey}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="relative">
            <pre className="bg-codebg border border-grid p-3 text-[11px] leading-relaxed overflow-x-auto font-mono text-text/90">
              {EXAMPLE_JSON}
            </pre>
            <button
              type="button"
              className="btn-ghost absolute top-2 right-2 !px-2 !py-1 text-xs"
              onClick={copyExample}
              data-testid="format-doc-copy"
            >
              {copied ? t("formatDoc.copied") : t("formatDoc.copy")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
