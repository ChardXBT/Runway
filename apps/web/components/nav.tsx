import Link from "next/link";

const links = [
  ["Dashboard", "/"],
  ["Review", "/review"],
  ["Queue", "/queue"],
  ["Catalogue", "/catalogue"],
  ["Profile", "/profile"],
  ["Settings", "/settings"],
  ["Activity", "/activity"],
] as const;

export function Nav() {
  return (
    <aside className="sidebar">
      <Link href="/" className="brand" aria-label="Leeway dashboard">
        <span className="brand-mark">L</span>
        <span>
          <strong>Leeway</strong>
          <small>Qlob studio</small>
        </span>
      </Link>
      <nav aria-label="Primary navigation">
        {links.map(([label, href]) => (
          <Link key={href} href={href}>
            {label}
          </Link>
        ))}
      </nav>
      <div className="publishing-lock">
        <span aria-hidden="true">●</span>
        Publishing disabled
      </div>
    </aside>
  );
}
