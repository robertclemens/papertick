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
  { href: "/history", label: "History", icon: "≡" },
  { href: "/statements", label: "Statements", icon: "▦" },
  { href: "/taxes", label: "Taxes", icon: "％" },
  { href: "/keys", label: "API Keys", icon: "⚿" },
  { href: "/scenarios", label: "Scenarios", icon: "◆" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];

export default function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [me, setMe] = useState<MeT | null>(null);

  useEffect(() => {
    api<MeT>("/auth/me").then(setMe).catch(() => setMe(null));
  }, [pathname]);

  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      router.push("/login");
      router.refresh();
    }
  }

  return (
    <ScenarioProvider>
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-40 flex w-56 flex-col border-r border-slate-800 bg-slate-900/60 backdrop-blur">
        <Link href="/" className="flex items-center gap-2 px-5 py-5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500 text-base font-bold text-slate-950">
            P
          </span>
          <span className="text-lg font-semibold tracking-tight text-slate-100">
            Paper<span className="text-emerald-400">Tick</span>
          </span>
        </Link>
        <nav className="flex-1 space-y-1 px-3 py-2">
          {NAV.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition ${
                  active
                    ? "bg-emerald-500/10 text-emerald-400"
                    : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                }`}
              >
                <span aria-hidden className="w-4 text-center">{item.icon}</span>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="pt-2">
          <ScenarioSwitcher />
        </div>
        {me && (
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
        )}
        <div className="border-t border-slate-800 p-3">
          <button
            onClick={logout}
            className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium text-slate-400 transition hover:bg-slate-800 hover:text-slate-200"
          >
            <span aria-hidden className="w-4 text-center">⏻</span>
            Sign out
          </button>
        </div>
      </aside>
      <main className="ml-56 min-h-screen flex-1 px-8 py-8">
        <div className="mx-auto max-w-6xl">{children}</div>
      </main>
    </div>
    </ScenarioProvider>
  );
}
