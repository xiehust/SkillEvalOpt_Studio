import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api";
import { LogoutButton } from "./components/AuthGate";
import Dashboard from "./pages/Dashboard";
import Skills from "./pages/Skills";
import SkillDetailPage from "./pages/SkillDetail";
import TaskSets from "./pages/TaskSets";
import TaskSetDetailPage from "./pages/TaskSetDetail";
import Evaluate from "./pages/Evaluate";
import Train from "./pages/Train";
import Jobs from "./pages/Jobs";
import JobDetailPage from "./pages/JobDetail";

const NAV_ITEMS = [
  { to: "/", labelKey: "nav.dashboard", crumb: "OVERVIEW" },
  { to: "/skills", labelKey: "nav.skills", crumb: "SKILLS" },
  { to: "/tasksets", labelKey: "nav.tasksets", crumb: "TASKSETS" },
  { to: "/evaluate", labelKey: "nav.evaluate", crumb: "EVALUATE" },
  { to: "/train", labelKey: "nav.train", crumb: "TRAIN" },
  { to: "/jobs", labelKey: "nav.jobs", crumb: "JOBS" },
];

function currentCrumb(pathname: string): string {
  if (pathname === "/") return NAV_ITEMS[0].crumb;
  const hit = NAV_ITEMS.slice(1).find((item) => pathname.startsWith(item.to));
  if (pathname.startsWith("/jobs")) return "JOBS";
  return hit ? hit.crumb : "OVERVIEW";
}

/** 顶栏健康指示灯 —— 30s 轮询 /api/health。 */
function useHealth(): boolean | null {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    const check = () =>
      api
        .health()
        .then((res) => alive && setOk(res.status === "ok"))
        .catch(() => alive && setOk(false));
    check();
    const timer = setInterval(check, 30_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);
  return ok;
}

function BrandGlyph() {
  return (
    <div className="w-[22px] h-[22px] shrink-0 bg-[#131008] border border-amber/60 grid grid-cols-2 grid-rows-2 p-[3px] gap-[2px]">
      <span className="bg-amber" />
      <span className="bg-transparent" />
      <span className="bg-transparent" />
      <span className="bg-amber/40" />
    </div>
  );
}

/** 顶栏语言切换(EN / 中文)—— launchpad .langswitch 样式,localStorage 记忆。 */
function LangSwitcher() {
  const { i18n } = useTranslation();
  const lang = i18n.resolvedLanguage ?? "zh-CN";
  const btn = "font-mono text-[10.5px] tracking-[0.05em] px-2.5 py-1 cursor-pointer";
  return (
    <div className="flex border border-line bg-panel" data-testid="lang-switcher">
      <button
        type="button"
        className={`${btn} ${!lang.startsWith("zh") ? "text-amber bg-amber/[.13]" : "text-faint hover:text-text"}`}
        onClick={() => void i18n.changeLanguage("en")}
      >
        EN
      </button>
      <button
        type="button"
        className={`${btn} ${lang.startsWith("zh") ? "text-amber bg-amber/[.13]" : "text-faint hover:text-text"}`}
        onClick={() => void i18n.changeLanguage("zh-CN")}
      >
        中文
      </button>
    </div>
  );
}

function Topbar() {
  const location = useLocation();
  const healthy = useHealth();
  const { t } = useTranslation("app");
  return (
    <header className="h-[52px] sticky top-0 z-50 flex items-center gap-5 px-5 border-b border-line bg-[rgba(11,14,13,.92)] backdrop-blur-[6px]">
      <div className="flex items-center gap-2.5 whitespace-nowrap">
        <BrandGlyph />
        <span className="[font-stretch:125%] font-extrabold tracking-[0.14em] text-[13px]">
          SKILLEVAL&amp;OPT <em className="not-italic text-amber">STUDIO</em>
        </span>
      </div>
      <span className="font-mono text-[11px] text-faint tracking-[0.06em] hidden sm:inline">
        CONSOLE / <b className="text-muted font-medium">{currentCrumb(location.pathname)}</b>
      </span>
      <div className="ml-auto flex items-center gap-2.5">
        <span
          className="font-mono text-[10.5px] text-muted border border-line bg-panel px-2.5 py-1 flex items-center gap-2"
          data-testid="syschip"
        >
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              healthy === false
                ? "bg-crit shadow-[0_0_6px_#D03B3B]"
                : "bg-good shadow-[0_0_6px_#0CA30C] pulse-dot"
            }`}
          />
          {healthy === false ? t("syschip.offline") : t("syschip.ok")}
        </span>
        <LangSwitcher />
        <LogoutButton />
      </div>
    </header>
  );
}

export default function App() {
  const { t } = useTranslation("app");
  return (
    <div className="min-h-screen">
      <Topbar />
      <div className="grid grid-cols-[216px_1fr] min-h-[calc(100vh-52px)]">
        <aside className="border-r border-line py-4 flex flex-col gap-0.5 sticky top-[52px] h-[calc(100vh-52px)]">
          <div className="font-mono text-[9.5px] tracking-[0.22em] text-faint px-5 pt-2 pb-1.5">CONSOLE</div>
          <nav className="flex-1">
            {NAV_ITEMS.map((item, index) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-5 py-[9px] text-[13px] font-medium border-l-2 ${
                    isActive
                      ? "border-amber text-amber bg-gradient-to-r from-amber/[.13] to-transparent"
                      : "border-transparent text-muted hover:text-text hover:bg-white/[.02]"
                  }`
                }
              >
                {({ isActive }) => (
                  <>
                    <span className={`font-mono text-[9.5px] w-5 ${isActive ? "text-amber" : "text-faint"}`}>
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    {t(item.labelKey)}
                  </>
                )}
              </NavLink>
            ))}
          </nav>
          <div className="mt-auto px-5 py-3.5 border-t border-line font-mono text-[9.5px] text-faint leading-[1.9]">
            <div className="truncate">
              HOST <b className="text-muted font-medium">{window.location.host}</b>
            </div>
            <div>
              MODE <b className="text-muted font-medium">SKILLEVAL + OPT</b>
            </div>
          </div>
        </aside>
        <main className="min-w-0">
          <div className="max-w-[1460px] mx-auto px-7 py-6 pb-16">
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/skills" element={<Skills />} />
              <Route path="/skills/:id" element={<SkillDetailPage />} />
              <Route path="/tasksets" element={<TaskSets />} />
              <Route path="/tasksets/:id" element={<TaskSetDetailPage />} />
              <Route path="/evaluate" element={<Evaluate />} />
              <Route path="/train" element={<Train />} />
              <Route path="/jobs" element={<Jobs />} />
              <Route path="/jobs/:id" element={<JobDetailPage />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  );
}
