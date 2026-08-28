import { marked, type Token, type Tokens } from "marked";
import { Fragment, type ReactNode, useMemo } from "react";

/**
 * Renders markdown by walking marked's token tree with React components.
 *
 * Deliberately not `innerHTML` + a sanitiser: assistant text is untrusted, and
 * building the DOM from tokens means no HTML string is ever parsed, so there is
 * no injection surface to sanitise in the first place. Raw HTML in the source
 * is shown as text rather than executed.
 */
export function Markdown({ text }: { text: string }) {
  const tokens = useMemo(() => {
    try {
      return marked.lexer(text);
    } catch {
      return null;
    }
  }, [text]);

  // A malformed stream fragment must still show the user their text.
  if (!tokens) return <div className="md">{text}</div>;
  return (
    <div className="md">
      <Nodes tokens={tokens} />
    </div>
  );
}

function Nodes({ tokens }: { tokens: Token[] | undefined }): ReactNode {
  if (!tokens) return null;
  return tokens.map((token, index) => <Node key={index} token={token} />);
}

function Node({ token }: { token: Token }): ReactNode {
  switch (token.type) {
    case "heading": {
      const t = token as Tokens.Heading;
      const Tag = `h${Math.min(t.depth, 6)}` as "h1";
      return (
        <Tag>
          <Nodes tokens={t.tokens} />
        </Tag>
      );
    }
    case "paragraph":
      return (
        <p>
          <Nodes tokens={(token as Tokens.Paragraph).tokens} />
        </p>
      );
    case "text": {
      const t = token as Tokens.Text;
      return t.tokens ? <Nodes tokens={t.tokens} /> : <>{t.text}</>;
    }
    case "strong":
      return (
        <strong>
          <Nodes tokens={(token as Tokens.Strong).tokens} />
        </strong>
      );
    case "em":
      return (
        <em>
          <Nodes tokens={(token as Tokens.Em).tokens} />
        </em>
      );
    case "del":
      return (
        <del>
          <Nodes tokens={(token as Tokens.Del).tokens} />
        </del>
      );
    case "codespan":
      return <code className="inline">{(token as Tokens.Codespan).text}</code>;
    case "code": {
      const t = token as Tokens.Code;
      return (
        <pre className="code">
          {t.lang ? <span className="lang">{t.lang}</span> : null}
          <code>{t.text}</code>
        </pre>
      );
    }
    case "list": {
      const t = token as Tokens.List;
      const items = t.items.map((item, index) => (
        <li key={index}>
          <Nodes tokens={item.tokens} />
        </li>
      ));
      return t.ordered ? <ol start={Number(t.start) || 1}>{items}</ol> : <ul>{items}</ul>;
    }
    case "list_item":
      return (
        <li>
          <Nodes tokens={(token as Tokens.ListItem).tokens} />
        </li>
      );
    case "blockquote":
      return (
        <blockquote>
          <Nodes tokens={(token as Tokens.Blockquote).tokens} />
        </blockquote>
      );
    case "link": {
      const t = token as Tokens.Link;
      // Only http(s) escapes to the browser; javascript:/data: are shown inert.
      const safe = /^https?:\/\//i.test(t.href);
      return safe ? (
        <a href={t.href} target="_blank" rel="noreferrer noopener">
          <Nodes tokens={t.tokens} />
        </a>
      ) : (
        <span>
          <Nodes tokens={t.tokens} />
        </span>
      );
    }
    case "table": {
      const t = token as Tokens.Table;
      return (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {t.header.map((cell, index) => (
                  <th key={index} style={{ textAlign: t.align[index] ?? undefined }}>
                    <Nodes tokens={cell.tokens} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {t.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, index) => (
                    <td key={index} style={{ textAlign: t.align[index] ?? undefined }}>
                      <Nodes tokens={cell.tokens} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    case "hr":
      return <hr />;
    case "br":
      return <br />;
    case "space":
      return null;
    case "escape":
      return <>{(token as Tokens.Escape).text}</>;
    default: {
      // html and anything unmapped: show the source, never execute it.
      const raw = (token as { raw?: string; text?: string }).text ?? (token as { raw?: string }).raw ?? "";
      return raw ? <Fragment>{raw}</Fragment> : null;
    }
  }
}
