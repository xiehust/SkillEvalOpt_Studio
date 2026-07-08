// Shared Prism syntax highlighting (PrismLight — only registered languages ship
// in the bundle) plus markdown-viewer link helpers used by the file previews.
import { Children, isValidElement, ReactElement, ReactNode } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { Components } from "react-markdown";
import oneDark from "react-syntax-highlighter/dist/esm/styles/prism/one-dark";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import c from "react-syntax-highlighter/dist/esm/languages/prism/c";
import cpp from "react-syntax-highlighter/dist/esm/languages/prism/cpp";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import diff from "react-syntax-highlighter/dist/esm/languages/prism/diff";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import toml from "react-syntax-highlighter/dist/esm/languages/prism/toml";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";

const LANGUAGES: Record<string, unknown> = {
  bash, c, cpp, css, diff, go, javascript, json, jsx,
  markup, python, rust, sql, toml, tsx, typescript, yaml,
};
for (const [name, grammar] of Object.entries(LANGUAGES)) {
  SyntaxHighlighter.registerLanguage(name, grammar);
}

const ALIASES: Record<string, string> = {
  sh: "bash", shell: "bash", zsh: "bash", console: "bash",
  py: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", yml: "yaml", html: "markup", xml: "markup", svg: "markup",
  rs: "rust", golang: "go", "c++": "cpp", h: "c", hpp: "cpp", cc: "cpp",
  patch: "diff",
};

/** Fence tag or file extension → registered Prism language ("" → unhighlighted). */
export function resolveLanguage(raw: string): string {
  const lang = raw.toLowerCase();
  const resolved = ALIASES[lang] ?? lang;
  return resolved in LANGUAGES ? resolved : "";
}

export function languageForFile(path: string): string {
  const name = path.split("/").pop() ?? "";
  const ext = name.includes(".") ? name.split(".").pop()! : "";
  return resolveLanguage(ext);
}

// Studio theme tokens (tailwind.config.js) — oneDark's own bg clashes with the palette.
const CODE_STYLE = {
  margin: 0,
  background: "#0E1524",
  border: "1px solid #2A3647",
  borderRadius: "0.375rem",
  fontSize: "0.75rem",
  fontFamily: '"IBM Plex Mono", ui-monospace, "Noto Sans Mono CJK SC", monospace',
};

export function CodeHighlight({
  language, code, maxHeight,
}: { language: string; code: string; maxHeight?: string }) {
  const resolved = resolveLanguage(language);
  return (
    <SyntaxHighlighter
      language={resolved || undefined}
      style={oneDark}
      customStyle={{ ...CODE_STYLE, maxHeight }}
      codeTagProps={{ style: { fontFamily: "inherit", background: "transparent" } }}
    >
      {code}
    </SyntaxHighlighter>
  );
}

/** Resolve a relative markdown href against the directory of the file being viewed. */
export function resolveRelative(baseFile: string, href: string): string {
  const clean = href.split(/[?#]/)[0];
  const segments = baseFile.split("/").slice(0, -1);
  for (const part of clean.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") segments.pop();
    else segments.push(part);
  }
  return segments.join("/");
}

/** Absolute URLs, fragments and site-absolute paths keep default anchor behavior. */
export const isExternalHref = (href: string) => /^([a-z][a-z0-9+.-]*:|#|\/)/i.test(href);

/** react-markdown `pre` renderer: fenced blocks → CodeHighlight (replaces the
 * wrapper pre entirely so we don't nest SyntaxHighlighter's pre inside it);
 * inline code keeps the default prose-dark chip styling. */
export const markdownPre: Components["pre"] = ({ node: _node, children, ...rest }) => {
  const child = Children.toArray(children)[0];
  if (isValidElement(child)) {
    const props = (child as ReactElement<{ className?: string; children?: ReactNode }>).props;
    const match = /language-([\w+.-]+)/.exec(props.className ?? "");
    const raw = props.children;
    const text = (Array.isArray(raw) ? raw.join("") : String(raw ?? "")).replace(/\n$/, "");
    return <CodeHighlight language={match?.[1] ?? ""} code={text} />;
  }
  return <pre {...rest}>{children}</pre>;
};
