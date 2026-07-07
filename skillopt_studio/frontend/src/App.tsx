import { NavLink, Route, Routes } from "react-router-dom";
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
  { to: "/", label: "总览", glyph: "◧" },
  { to: "/skills", label: "技能库", glyph: "❖" },
  { to: "/tasksets", label: "任务集", glyph: "☰" },
  { to: "/evaluate", label: "发起评估", glyph: "▶" },
  { to: "/train", label: "发起训练", glyph: "↻" },
  { to: "/jobs", label: "任务管理", glyph: "≡" },
];

export default function App() {
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-line bg-panel/70 backdrop-blur flex flex-col">
        <div className="px-5 py-5 border-b border-line">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-sm bg-bg border border-line grid grid-cols-2 grid-rows-2 p-1 gap-0.5">
              <span className="bg-green rounded-[1px]" />
              <span className="bg-transparent" />
              <span className="bg-transparent" />
              <span className="bg-cyan rounded-[1px]" />
            </div>
            <div>
              <div className="font-semibold tracking-wide leading-tight">SkillEval&amp;Opt</div>
              <div className="text-[10px] uppercase tracking-[0.3em] text-green leading-tight">Studio</div>
            </div>
          </div>
        </div>
        <nav className="flex-1 py-3">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 px-5 py-2.5 text-sm border-l-2 transition-colors ${
                  isActive
                    ? "border-green bg-panel2 text-text font-medium"
                    : "border-transparent text-muted hover:text-text hover:bg-panel2/50"
                }`
              }
            >
              <span className="text-xs w-4 text-center opacity-80">{item.glyph}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="px-5 py-4 border-t border-line text-[11px] text-muted font-mono">
          {window.location.host}
        </div>
      </aside>
      <main className="flex-1 min-w-0">
        <div className="max-w-[1400px] mx-auto px-8 py-6">
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
  );
}
