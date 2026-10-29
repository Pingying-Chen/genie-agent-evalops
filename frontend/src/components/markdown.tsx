// Minimal Markdown for Genie reports: headings, bullet lists, **bold**, `code`, paragraphs.
// Builds React nodes (never innerHTML), so report text cannot inject markup. Figures listed in
// `highlight` are wrapped in <mark> wherever they appear.
import type { ReactNode } from "react";

const CITATION = /\\?\[\\?\[?(\d+)\]?\([^)]*\)\\?\]?/g;

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function inline(text: string, highlight: RegExp | null, key: string): ReactNode[] {
  // Split on **bold** and `code` first, then mark highlighted figures inside each piece.
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((piece, i) => {
    const k = `${key}-${i}`;
    if (piece.startsWith("**") && piece.endsWith("**")) return <strong key={k}>{mark(piece.slice(2, -2), highlight, k)}</strong>;
    if (piece.startsWith("`") && piece.endsWith("`")) return <code key={k}>{piece.slice(1, -1)}</code>;
    return <span key={k}>{mark(piece, highlight, k)}</span>;
  });
}

function mark(text: string, highlight: RegExp | null, key: string): ReactNode[] {
  if (!highlight) return [text];
  return text.split(highlight).map((part, i) =>
    i % 2 === 1 ? <mark key={`${key}-m${i}`} title="no source in any step result">{part}</mark> : part,
  );
}

export function Markdown({ text, highlight = [] }: { text: string; highlight?: string[] }) {
  const pattern = highlight.length ? new RegExp(`(${highlight.map(escapeRegex).join("|")})`) : null;
  // Genie sometimes runs a heading on after a citation ("…[1]### Breakdown"): start it on its own line.
  const lines = text
    .replace(CITATION, " [$1] ")
    .replace(/([^\n])[ \t]*(#{2,6} )/g, "$1\n$2")
    .split("\n");
  const blocks: ReactNode[] = [];
  let list: ReactNode[] = [];
  const flush = () => {
    if (list.length) blocks.push(<ul key={`ul-${blocks.length}`}>{list}</ul>);
    list = [];
  };
  lines.forEach((raw, i) => {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      list.push(<li key={i}>{inline(bullet[1], pattern, `l${i}`)}</li>);
      return;
    }
    flush();
    if (heading) blocks.push(<h4 key={i}>{inline(heading[2], pattern, `h${i}`)}</h4>);
    else if (line.trim()) blocks.push(<p key={i}>{inline(line, pattern, `p${i}`)}</p>);
  });
  flush();
  return <div className="report text-[0.95rem] leading-relaxed">{blocks}</div>;
}
