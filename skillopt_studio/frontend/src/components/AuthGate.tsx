import { FormEvent, ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../api";
import { Spinner } from "./ui";

/**
 * 登录门:仅当后端启用鉴权(prod 模式,STUDIO_AUTH_PASSWORD 已设置)且当前
 * 会话未认证时拦截整个应用;任何 API 返回 401 会触发 `studio-unauthorized`
 * 事件,把界面切回登录页(会话过期场景)。本地 dev 无感知。
 */
export default function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"loading" | "login" | "ready">("loading");

  useEffect(() => {
    api
      .authStatus()
      .then((status) => setState(status.auth_required && !status.authenticated ? "login" : "ready"))
      .catch(() => setState("ready")); // 状态接口不可达时放行,由页面自身报连接错误
  }, []);

  useEffect(() => {
    const onUnauthorized = () => setState("login");
    window.addEventListener("studio-unauthorized", onUnauthorized);
    return () => window.removeEventListener("studio-unauthorized", onUnauthorized);
  }, []);

  if (state === "loading") {
    return (
      <div className="min-h-screen grid place-items-center">
        <Spinner />
      </div>
    );
  }
  if (state === "login") {
    return <LoginPage onSuccess={() => setState("ready")} />;
  }
  return <>{children}</>;
}

function LoginPage({ onSuccess }: { onSuccess: () => void }) {
  const { t } = useTranslation("app");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError(t("login.missing"));
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await api.login(username.trim(), password);
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? t("login.badCredentials") : String(err));
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen grid place-items-center px-4">
      <form
        onSubmit={onSubmit}
        noValidate
        className="w-full max-w-sm bg-panel border border-amber shadow-[0_24px_80px_-24px_rgba(0,0,0,.8)] p-8 space-y-5"
        data-testid="login-form"
      >
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 shrink-0 bg-[#131008] border border-amber/60 grid grid-cols-2 grid-rows-2 p-1 gap-0.5">
            <span className="bg-amber" />
            <span className="bg-transparent" />
            <span className="bg-transparent" />
            <span className="bg-amber/40" />
          </div>
          <div>
            <div className="[font-stretch:120%] font-extrabold tracking-[0.1em] text-[14px] leading-tight">
              SKILLEVAL&amp;OPT
            </div>
            <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-amber leading-tight mt-0.5">
              Studio
            </div>
          </div>
        </div>
        <p className="font-mono text-[11px] tracking-[0.04em] text-muted leading-relaxed">
          {t("login.notice")}
        </p>
        <div>
          <label className="label">{t("login.username")}</label>
          <input
            className="input"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            data-testid="login-username"
          />
        </div>
        <div>
          <label className="label">{t("login.password")}</label>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            data-testid="login-password"
          />
        </div>
        {error && (
          <div className="text-sm text-critText" data-testid="login-error">
            {error}
          </div>
        )}
        <button type="submit" className="btn-primary w-full" disabled={submitting} data-testid="login-submit">
          {submitting ? t("login.submitting") : t("login.submit")}
        </button>
      </form>
    </div>
  );
}

/** 侧边栏“退出登录”按钮 —— 仅在后端启用鉴权时渲染。 */
export function LogoutButton() {
  const { t } = useTranslation("app");
  const [authRequired, setAuthRequired] = useState(false);

  useEffect(() => {
    api.authStatus().then((status) => setAuthRequired(status.auth_required)).catch(() => {});
  }, []);

  if (!authRequired) return null;
  const onLogout = async () => {
    try {
      await api.logout();
    } finally {
      window.dispatchEvent(new Event("studio-unauthorized"));
    }
  };
  return (
    <button
      className="font-mono text-[10.5px] tracking-[0.05em] text-muted border border-line bg-panel px-2.5 py-1 hover:text-critText hover:border-crit/45"
      onClick={onLogout}
      data-testid="logout-button"
      title={t("login.logout")}
    >
      {t("login.logout")}
    </button>
  );
}
