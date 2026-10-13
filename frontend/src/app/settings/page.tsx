"use client";
import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { DeepSeekKeySection } from "@/components/settings/DeepSeekKeySection";

/**
 * Wire shape of GET / PUT /config (`CompanyConfigResponse` in the backend).
 *
 * `timezone` / `daily_cycle_hour` are `| null` on purpose: the `company_config` columns carry no
 * `NOT NULL`, the service layer documents a stored NULL as "no value" (never the 6-hour default), and
 * the response model now serves that null — the strict `string` / `number` here was an assumption the
 * server does not make. The inputs already render null as an empty box.
 *
 * Exported (type-only — erased at build time, invisible to Next's page-export validator) so this
 * page's test can pin the nullable contract at COMPILE time: a fixture declaring `timezone: null`
 * stops compiling the moment one side drops the null.
 */
export type SettingsConfig = {
  name: string;
  mission: string | null;
  vision: string | null;
  description: string | null;
  target_market: string | null;
  value_prop: string | null;
  website_url: string | null;
  industry: string | null;
  product_type: string | null;
  timezone: string | null;
  daily_cycle_hour: number | null;
};

/**
 * The repo's house error idiom: a refusal shows its code, its message and — when the server supplied
 * one — the fix hint (`ApiError.display`); anything else shows its own message. Before this page
 * adopted it, a refused save was completely invisible: the button flipped back and nothing happened,
 * so "saved" and "refused" looked identical.
 */
function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

export default function SettingsPage() {
  const [config, setConfig] = useState<SettingsConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<SettingsConfig>("/config")
      .then(setConfig)
      .catch((e: unknown) => setError(errorText(e)))
      .finally(() => setLoading(false));
  }, []);

  const save = async () => {
    if (!config) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await api.put<SettingsConfig>("/config", config);
      setConfig(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (e: unknown) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="p-6 text-gray-400">Loading…</div>;
  if (!config)
    return (
      <div className="p-6 space-y-2">
        <p className="text-red-400">Failed to load config</p>
        {/* The real reason, in the server's own words — the generic line above carries none. */}
        {error && (
          <p className="text-red-400" role="alert">
            {error}
          </p>
        )}
      </div>
    );

  const field = (label: string, key: keyof SettingsConfig, type: "text" | "number" = "text") => (
    <div key={key}>
      <label className="block text-sm text-gray-400 mb-1">{label}</label>
      <input
        type={type}
        value={(config[key] as string | number | null) ?? ""}
        onChange={(e) =>
          setConfig((c) => {
            if (!c) return c;
            const raw = e.target.value;
            if (type !== "number") return { ...c, [key]: raw };
            // A cleared box means "no value", NOT midnight: `Number("")` is 0 (ECMA-262 §7.1.4.1), so
            // the old one-liner silently stored daily_cycle_hour = 0. Keep the previous number, and
            // never transmit NaN (it would serialize to JSON null and silently clear the field).
            if (raw.trim() === "") return c;
            const parsed = Number(raw);
            return Number.isFinite(parsed) ? { ...c, [key]: parsed } : c;
          })
        }
        className="w-full bg-gray-700 text-white rounded px-3 py-2 text-sm border border-gray-600 focus:border-indigo-500 focus:outline-none"
      />
    </div>
  );

  return (
    <div className="p-6 max-w-2xl space-y-6">
      <h1 className="text-2xl font-bold text-white">Settings</h1>

      {saved && (
        <div className="bg-green-900/50 border border-green-500 text-green-300 px-4 py-2 rounded text-sm">
          Settings saved successfully
        </div>
      )}

      {/* A refused PUT used to be invisible (the button flipped back and nothing else happened, so
          "saved" and "refused" looked the same). The banner carries the server's own refusal. */}
      {error && (
        <p className="text-red-400 text-sm" role="alert">
          {error}
        </p>
      )}

      <div className="bg-gray-800 rounded-lg p-5 space-y-4">
        <h2 className="text-white font-semibold">Company Profile</h2>
        {field("Company Name", "name")}
        {field("Industry", "industry")}
        {field("Product Type", "product_type")}
        {field("Website URL", "website_url")}
        {field("Mission", "mission")}
        {field("Vision", "vision")}
        {field("Description", "description")}
        {field("Target Market", "target_market")}
        {field("Value Proposition", "value_prop")}
      </div>

      <div className="bg-gray-800 rounded-lg p-5 space-y-4">
        <h2 className="text-white font-semibold">Scheduler</h2>
        {field("Timezone", "timezone")}
        {field("Morning Cycle Hour (company-local)", "daily_cycle_hour", "number")}
      </div>

      {/* DeepSeek key lifecycle: save / change / remove / verify. Independent of the
          company-profile form below (it has its own endpoints and its own save button). */}
      <DeepSeekKeySection />

      <button
        onClick={save}
        disabled={saving}
        className="px-6 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-md text-sm"
      >
        {saving ? "Saving…" : "Save Settings"}
      </button>
    </div>
  );
}
