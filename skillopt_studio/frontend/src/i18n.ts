// i18n bootstrap — mirrors agentcore_launchpad's setup (localStorage-persisted,
// browser-detected, zh-CN fallback so the existing Chinese-first experience is unchanged).
import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import enApp from "./locales/en/app.json";
import enCommon from "./locales/en/common.json";
import enDashboard from "./locales/en/dashboard.json";
import enJobs from "./locales/en/jobs.json";
import enSkills from "./locales/en/skills.json";
import enTasksets from "./locales/en/tasksets.json";
import enWizards from "./locales/en/wizards.json";
import zhApp from "./locales/zh-CN/app.json";
import zhCommon from "./locales/zh-CN/common.json";
import zhDashboard from "./locales/zh-CN/dashboard.json";
import zhJobs from "./locales/zh-CN/jobs.json";
import zhSkills from "./locales/zh-CN/skills.json";
import zhTasksets from "./locales/zh-CN/tasksets.json";
import zhWizards from "./locales/zh-CN/wizards.json";

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: {
        common: enCommon,
        app: enApp,
        dashboard: enDashboard,
        skills: enSkills,
        tasksets: enTasksets,
        wizards: enWizards,
        jobs: enJobs,
      },
      "zh-CN": {
        common: zhCommon,
        app: zhApp,
        dashboard: zhDashboard,
        skills: zhSkills,
        tasksets: zhTasksets,
        wizards: zhWizards,
        jobs: zhJobs,
      },
    },
    fallbackLng: "zh-CN",
    supportedLngs: ["en", "zh-CN"],
    defaultNS: "common",
    interpolation: { escapeValue: false },
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      caches: ["localStorage"],
    },
  });

i18n.on("languageChanged", (lng) => {
  document.documentElement.lang = lng;
});

/** 日期本地化:zh-CN 保持原有格式,en 用 en-US。 */
export function dateLocale(): string {
  return (i18n.resolvedLanguage ?? "zh-CN").startsWith("zh") ? "zh-CN" : "en-US";
}

export default i18n;
