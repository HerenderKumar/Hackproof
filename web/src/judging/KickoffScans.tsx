// The organiser's view of the kickoff scan (M9): every team's devices with what each scan could
// not read, members still missing a scan, and the approved starter kits. No file content.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { get, remove, upload } from "@/api";
import { Button, ErrorNotice, Field, Section, Spinner, Tag } from "@/components/ui";
import { when } from "@/time";
import type { BaselinesFeed, DeviceDetail, StarterOut } from "@/types";

const STATE: Record<DeviceDetail["state"], string> = {
  missing: "Not scanned",
  uploading: "Uploading",
  complete: "Scanned",
  incomplete: "Scanned, incomplete",
};

function Device({ device }: { device: DeviceDetail }) {
  const [open, setOpen] = useState(false);
  const skipped = Object.entries(device.summary?.skipped ?? {}).filter(([, n]) => n > 0);
  return (
    <li className="text-sm">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-medium">{device.label}</span>
        <Tag>{STATE[device.state]}</Tag>
        {device.late && <Tag>late</Tag>}
        {device.sealed_at && (
          <span className="text-xs text-neutral-500">
            {device.files ?? 0} files · sealed {when(device.sealed_at)}
            {device.platform && ` · ${device.platform}`}
          </span>
        )}
        {(device.summary || device.unreadable.length > 0) && (
          <button type="button" className="text-xs text-indigo-700 underline" onClick={() => setOpen(!open)}>
            {open ? "less" : "details"}
          </button>
        )}
      </div>
      {device.incomplete_reasons.length > 0 && (
        <p className="text-xs text-neutral-600">{device.incomplete_reasons.join(" · ")}</p>
      )}
      {open && (
        <div className="mt-1 space-y-1 rounded-md border border-neutral-200 bg-neutral-50 p-2 text-xs text-neutral-700">
          {device.summary && (
            <p>
              Scanned {device.summary.roots.join(", ")}: {device.summary.files_found} files, {device.summary.keys_blanked} keys
              blanked{skipped.length > 0 && <>; skipped {skipped.map(([k, n]) => `${k.replace("_", " ")} ${n}`).join(", ")}</>}
              {device.summary.size_limit_reached && "; the size limit was reached"}.
            </p>
          )}
          {device.unreadable.length > 0 && (
            <ul className="space-y-0.5">
              {device.unreadable.map((item) => (
                <li key={`${item.kind}:${item.path}`} className="break-all">
                  <span className="font-mono">{item.path}</span> ({item.kind}): {item.reason}
                </li>
              ))}
              {device.unreadable_more > 0 && <li>…and {device.unreadable_more} more</li>}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

function Starters({ eventId }: { eventId: string }) {
  const queryClient = useQueryClient();
  const key = ["starters", eventId];
  const starters = useQuery({ queryKey: key, queryFn: () => get<StarterOut[]>(`/events/${eventId}/starters`) });
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const add = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("name", name.trim());
      if (file) form.append("file", file);
      return upload<StarterOut>(`/events/${eventId}/starters`, form);
    },
    onSuccess: () => {
      setName("");
      setFile(null);
      void queryClient.invalidateQueries({ queryKey: key });
    },
  });
  const drop = useMutation({
    mutationFn: (id: string) => remove(`/events/${eventId}/starters/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: key }),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    add.mutate();
  }
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-semibold text-neutral-900">Approved starter code</h3>
      <p className="text-xs text-neutral-500">
        Code from a starter kit you hand out is left out of every team's comparison. Only its fingerprints are kept.
      </p>
      {starters.data?.length ? (
        <ul className="space-y-1 text-sm">
          {starters.data.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{s.name}</span>
              <span className="text-xs text-neutral-500">
                {s.files} files · added {when(s.created_at)}
              </span>
              <Button variant="secondary" onClick={() => drop.mutate(s.id)} busy={drop.isPending && drop.variables === s.id}>
                Remove
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-neutral-600">{starters.isPending ? "Loading…" : "None."}</p>
      )}
      <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
        <Field className="min-w-48 flex-1" label="Starter name" value={name} onChange={setName} max={100} />
        <label className="text-sm">
          <span className="block font-medium text-neutral-800">Archive (.zip or .tar.gz)</span>
          <input
            type="file"
            accept=".zip,.gz,.tgz,application/zip,application/gzip"
            className="mt-1 block text-sm"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </label>
        <Button type="submit" busy={add.isPending} disabled={!name.trim() || !file}>
          Add starter
        </Button>
      </form>
      <ErrorNotice error={add.error ?? drop.error} />
    </div>
  );
}

export function KickoffScans({ eventId }: { eventId: string }) {
  const feed = useQuery({
    queryKey: ["baselines", eventId],
    queryFn: () => get<BaselinesFeed>(`/events/${eventId}/baselines`),
    refetchInterval: 15000,
  });
  return (
    <Section title="Kickoff scans">
      {feed.isPending ? (
        <Spinner label="Loading the scans" />
      ) : feed.isError ? (
        <ErrorNotice error={feed.error} />
      ) : (
        <ul className="divide-y divide-neutral-200">
          {feed.data.teams.map((team) => (
            <li key={team.team_id} className="py-2">
              <p className="text-sm font-medium text-neutral-900">
                {team.team_name}
                <span className="ml-2 text-xs font-normal text-neutral-500">
                  {team.members === null
                    ? "members not given"
                    : `${team.members} members, ${team.missing} without a scan`}
                </span>
              </p>
              {team.devices.length ? (
                <ul className="mt-1 space-y-1">
                  {team.devices.map((device) => (
                    <Device key={device.id} device={device} />
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-neutral-500">No device added.</p>
              )}
            </li>
          ))}
        </ul>
      )}
      <Starters eventId={eventId} />
    </Section>
  );
}
