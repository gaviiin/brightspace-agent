import { useQuery } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { getSettings } from "../api/client";

export function SettingsPage() {
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: getSettings });

  return (
    <div className="mx-auto max-w-2xl p-6">
      <div className="mb-4 flex items-center gap-3">
        <Link
          to="/"
          className="text-sm text-neutral-500 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
        >
          ← Courses
        </Link>
      </div>
      <h1 className="mb-4 text-xl font-semibold text-neutral-900 dark:text-neutral-100">Settings</h1>

      {settingsQuery.isLoading && (
        <p className="text-neutral-500 dark:text-neutral-400">Loading settings…</p>
      )}
      {settingsQuery.isError && (
        <p className="text-red-600 dark:text-red-400">Couldn't reach the backend. Is it running?</p>
      )}

      {settingsQuery.data && (
        <div className="space-y-6">
          <section>
            <h2 className="mb-1 text-sm font-semibold text-neutral-800 dark:text-neutral-100">
              Pairing token
            </h2>
            <p className="mb-2 text-sm text-neutral-500 dark:text-neutral-400">
              Paste this into the BrightSpace Agent browser extension's popup to pair it with this
              backend.
            </p>
            <PairingTokenField token={settingsQuery.data.pairingToken} />
          </section>

          <section>
            <h2 className="mb-1 text-sm font-semibold text-neutral-800 dark:text-neutral-100">Data</h2>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-neutral-500 dark:text-neutral-400">Data directory</dt>
              <dd className="truncate font-mono text-neutral-800 dark:text-neutral-200">
                {settingsQuery.data.dataDir}
              </dd>
            </dl>
          </section>

          <section>
            <h2 className="mb-1 text-sm font-semibold text-neutral-800 dark:text-neutral-100">Models</h2>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-neutral-500 dark:text-neutral-400">Fast</dt>
              <dd className="font-mono text-neutral-800 dark:text-neutral-200">
                {settingsQuery.data.models.fast}
              </dd>
              <dt className="text-neutral-500 dark:text-neutral-400">Smart</dt>
              <dd className="font-mono text-neutral-800 dark:text-neutral-200">
                {settingsQuery.data.models.smart}
              </dd>
              <dt className="text-neutral-500 dark:text-neutral-400">Max cost / run</dt>
              <dd className="text-neutral-800 dark:text-neutral-200">
                ${settingsQuery.data.maxCostUsdPerRun.toFixed(2)}
              </dd>
            </dl>
          </section>

          <section>
            <h2 className="mb-2 text-sm font-semibold text-neutral-800 dark:text-neutral-100">Status</h2>
            <div className="flex flex-wrap gap-2">
              <StatusBadge
                label={settingsQuery.data.mockLlm ? "Mock mode (LLM calls are free)" : "Live LLM calls"}
                tone={settingsQuery.data.mockLlm ? "amber" : "green"}
              />
              <StatusBadge
                label={
                  settingsQuery.data.apiKeyConfigured ? "Anthropic API key configured" : "No API key configured"
                }
                tone={settingsQuery.data.apiKeyConfigured ? "green" : "amber"}
              />
            </div>
            {!settingsQuery.data.apiKeyConfigured && !settingsQuery.data.mockLlm && (
              <p className="mt-2 text-sm text-neutral-500 dark:text-neutral-400">
                Set <code className="font-mono">ANTHROPIC_API_KEY</code> (or{" "}
                <code className="font-mono">BSA_ANTHROPIC_API_KEY</code>) in the backend's environment
                and restart it to run a real pipeline.
              </p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function PairingTokenField({ token }: { token: string }) {
  const [copied, setCopied] = useState(false);

  return (
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-md border border-neutral-200 bg-neutral-50 px-2 py-1.5 text-sm dark:border-neutral-800 dark:bg-neutral-900">
        {token}
      </code>
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard.writeText(token).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        }}
        className="flex shrink-0 items-center gap-1 rounded-md border border-neutral-300 px-2 py-1.5 text-sm text-neutral-700 transition hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
      >
        {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function StatusBadge({ label, tone }: { label: string; tone: "green" | "amber" }) {
  const toneClasses =
    tone === "green"
      ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300"
      : "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300";
  return <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${toneClasses}`}>{label}</span>;
}
