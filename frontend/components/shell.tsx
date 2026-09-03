"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, MeT } from "@/lib/api";
import ScenarioSwitcher from "@/components/scenario-switcher";
import { ScenarioProvider } from "@/components/scenario-context";

const NAV = [
  { href: "/", label: "Dashboard", icon: "◧" },
  { href: "/accounts", label: "Accounts", icon: "▤" },
  { href: "/trade", label: "Trade", icon: "⇄" },
  { href: "/options", label: "Options", icon: "⌥" },
  { href: "/schedules", label: "Auto-Invest", icon: "↻" },
  { href: "/performance", label: "Performance", icon: "◪" },
  { href: "/history", label: "History", icon: "≡" },
  { href: "/statements", label: "Statements", icon: "▦" },
  { href: "/taxes", label: "Taxes", icon: "％" },
  { href: "/scenarios", label: "Scenarios", icon: "◆" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];

function Wordmark({ className = "" }: { className?: string }) {
  return (
    <Link href="/" className={`flex items-center gap-2 ${className}`}>
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-500 text-base font-bold text-slate-950">
        P
      </span>
      <span className="text-lg font-semibold tracking-tight text-slate-100">
        Paper<span className="text-emerald-400">Tick</span>
      </span>
    </Link>
  );
}

/** The app frame.
 *
 *  One nav list, rendered in two places: a rail that is always there from
 *  `lg` up, and a drawer below it. They share `NAV` and the same markup, so a
 *  page added to one is in the other — a separate "mobile menu" is how the two
 *  drift apart.
 *
 *  The drawer is closed on navigation and on Escape, and while it is open the
 *  body cannot scroll behind it. Hit targets are 44px on the touch path, which
 *  is what a finger actually needs; the rail keeps the denser sizing a pointer
 *  can use.
 */
export default function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [me, setMe] = useState<MeT | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    api<MeT>("/auth/me").then(setMe).catch(() => setMe(null));
  }, [pathname]);

  // A route change is the drawer's cue to get out of the way.
  useEffect(() => { setOpen(false); }, [pathname]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", onKey);
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
    };
  }, [open]);

  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      router.push("/login");
      router.refresh();
    }
  }

  // 44px targets on touch, the denser rail sizing once there is a pointer.
  const TOUCH = "min-h-11 py-2.5 lg:min-h-0 lg:py-2";

  const navLinks = NAV.map((item) => {
    const active =
      item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
    return (
      <Link
        key={item.href}
        href={item.href}
        aria-current={active ? "page" : undefined}
        className={`flex items-center gap-3 rounded-lg px-3 text-sm font-medium transition ${TOUCH} ${
          active
            ? "bg-emerald-500/10 text-emerald-400"
            : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
        }`}
      >
        <span aria-hidden className="w-4 text-center">{item.icon}</span>
        {item.label}
      </Link>
    );
  });

  const account = me && (
    <div className="px-3 pb-1">
      <Link
        href="/settings"
        className="block rounded-lg px-3 py-2 transition hover:bg-slate-800"
        title="Profile & settings"
      >
        <div className="truncate text-sm font-medium text-slate-200">{me.full_name}</div>
        <div className="truncate text-xs text-slate-500">{me.email}</div>
      </Link>
    </div>
  );

  const signOut = (
    <div className="border-t border-slate-800 p-3">
      <button
        onClick={logout}
        className={`flex w-full items-center gap-3 rounded-lg px-3 text-sm font-medium text-slate-400 transition hover:bg-slate-800 hover:text-slate-200 ${TOUCH}`}
      >
        <span aria-hidden className="w-4 text-center">⏻</span>
        Sign out
      </button>
    </div>
  );

  return (
    <ScenarioProvider>
      <div className="min-h-screen">
        {/* Top bar: the drawer's handle, and the only place the wordmark
            appears on a small screen. Sticky, so the menu is always a thumb
            away instead of a scroll to the top. */}
        <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-slate-800 bg-slate-950/90 px-4 py-3 backdrop-blur lg:hidden">
          <button
            type="button"
            onClick={() => setOpen(true)}
            aria-label="Open menu"
            aria-expanded={open}
            aria-controls="app-nav"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-slate-800 text-slate-300 transition hover:bg-slate-800 hover:text-slate-100"
          >
            <span aria-hidden className="text-lg leading-none">≡</span>
          </button>
          <Wordmark className="min-w-0" />
        </header>

        <div className="lg:flex lg:items-start">
          {open && (
            <div
              className="fixed inset-0 z-40 bg-slate-950/70 lg:hidden"
              onClick={() => setOpen(false)}
              aria-hidden
            />
          )}

          <aside
            id="app-nav"
            /* Off-canvas below lg; from lg it is `sticky`, which keeps it in the
               flex flow (so `main` is laid out beside it rather than under it)
               while still holding position as the page scrolls. */
            className={`fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] shrink-0 flex-col overflow-y-auto overscroll-contain border-r border-slate-800 bg-slate-900 transition-transform duration-200 lg:sticky lg:inset-auto lg:top-0 lg:z-30 lg:h-screen lg:w-56 lg:max-w-none lg:translate-x-0 lg:bg-slate-900/60 lg:backdrop-blur ${
              open ? "translate-x-0" : "-translate-x-full"
            }`}
          >
            <div className="flex items-center justify-between gap-2 px-5 py-5">
              <Wordmark />
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close menu"
                className="flex h-9 w-9 items-center justify-center rounded-lg text-slate-500 transition hover:bg-slate-800 hover:text-slate-200 lg:hidden"
              >
                <span aria-hidden>✕</span>
              </button>
            </div>
            <nav className="flex-1 space-y-1 px-3 py-2">{navLinks}</nav>
            <div className="pt-2">
              <ScenarioSwitcher />
            </div>
            {account}
            {signOut}
          </aside>

          {/* min-w-0 is what stops a wide table from widening the whole page:
              without it a flex child sizes to its content and the body scrolls
              sideways instead of the table doing it. */}
          <main className="min-h-screen min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
            <div className="mx-auto w-full max-w-6xl">{children}</div>
          </main>
        </div>
      </div>
    </ScenarioProvider>
  );
}
