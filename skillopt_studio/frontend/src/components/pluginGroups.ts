import { SkillInfo } from "../api";

export interface PluginGroup {
  key: string;
  name: string;
  source: string;
  skills: SkillInfo[];
}

export function buildPluginGroups(skills: SkillInfo[]): PluginGroup[] {
  const grouped = new Map<string, PluginGroup>();
  for (const skill of skills) {
    if (!skill.plugin) continue;
    const key = `${skill.source}::${skill.plugin}`;
    const group = grouped.get(key) ?? {
      key,
      name: skill.plugin,
      source: skill.source,
      skills: [],
    };
    group.skills.push(skill);
    grouped.set(key, group);
  }
  return [...grouped.values()]
    .filter((group) => group.skills.length >= 2)
    .map((group) => ({
      ...group,
      skills: [...group.skills].sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function filterPluginGroups(
  groups: PluginGroup[],
  query: string,
): PluginGroup[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return groups;
  return groups.filter(
    (group) =>
      group.name.toLowerCase().includes(normalized)
      || group.skills.some(
        (skill) =>
          skill.name.toLowerCase().includes(normalized)
          || skill.id.toLowerCase().includes(normalized),
      ),
  );
}
