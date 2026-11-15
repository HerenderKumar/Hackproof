// The kickoff scan on the team overview (M9): each member's device, and how to scan it.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { remove, send } from "@/api";
import { Button, ErrorNotice, Field, Mono, Section, Spinner, Tag } from "@/components/ui";
import { keys, useDevices } from "@/queries";
import { isPast, useNow, when } from "@/time";
import type { DeviceCreated, DeviceOut } from "@/types";

const STATE: Record<DeviceOut["state"], string> = {
  missing: "Not scanned",
  uploading: "Uploading",
  complete: "Scanned",
  incomplete: "Scanned, incomplete",
};

function Device({ device }: { device: DeviceOut }) {
  const queryClient = useQueryClient();
  const drop = useMutation({
    mutationFn: () => remove(`/team/devices/${device.id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.devices }),
  });
  const removable = device.state === "missing" || device.state === "uploading";
  return (
    <li className="rounded-md border border-neutral-200 px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-neutral-900">{device.label}</span>
          <Tag>{STATE[device.state]}</Tag>
          {device.late && <Tag>late</Tag>}
          {(device.state === "complete" || device.state === "incomplete") && !device.indexed && (
            <Spinner small label="being indexed" />
          )}
        </div>
        {removable && (
          <Button variant="secondary" onClick={() => drop.mutate()} busy={drop.isPending}>
            Remove
          </Button>
        )}
      </div>
      {device.sealed_at && (
        <p className="mt-1 text-xs text-neutral-600">
          {device.files ?? 0} files, sealed {when(device.sealed_at)}
        </p>
      )}
      {device.incomplete_reasons.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-xs text-neutral-600">
          {device.incomplete_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
      <ErrorNotice error={drop.error} />
    </li>
  );
}

function Created({ created }: { created: DeviceCreated }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(created.command);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="space-y-2 rounded-md border border-neutral-300 bg-neutral-50 p-3">
      <p className="text-sm font-medium text-neutral-900">
        Scan "{created.device.label}". This code is shown once.
      </p>
      <p className="text-sm text-neutral-700">
        Download the scanner (one Python file you can read first):{" "}
        <a className="font-medium text-indigo-700 underline" href={created.download_url}>
          hackproof_scan.py
        </a>
        , then run, in the folder you saved it to:
      </p>
      <pre className="overflow-x-auto rounded border border-neutral-200 bg-white p-2 font-mono text-xs text-neutral-800">
        {created.command}
      </pre>
      <Button variant="secondary" onClick={copy}>
        {copied ? "Copied" : "Copy the command"}
      </Button>
    </div>
  );
}

export function KickoffScan({ serverNow }: { serverNow: string }) {
  const devices = useDevices();
  const now = useNow(serverNow);
  const queryClient = useQueryClient();
  const [label, setLabel] = useState("");
  const [created, setCreated] = useState<DeviceCreated | null>(null);
  const add = useMutation({
    mutationFn: (body: { label: string }) => send<DeviceCreated>("POST", "/team/devices", body),
    onSuccess: (made) => {
      setCreated(made);
      setLabel("");
      void queryClient.invalidateQueries({ queryKey: keys.devices });
    },
  });

  if (!devices.data) {
    return (
      <Section title="Kickoff scan">
        {devices.isError ? <ErrorNotice error={devices.error} /> : <Spinner label="Loading" />}
      </Section>
    );
  }
  const view = devices.data;

  function submit(event: FormEvent) {
    event.preventDefault();
    add.mutate({ label: label.trim() });
  }

  return (
    <Section title="Kickoff scan">
      <p className="text-sm text-neutral-700">
        Before kickoff, each of you runs HackProof's scanner on your own device. It reads only files ending in{" "}
        {view.extensions.join(", ")}, skips dependency and system folders, blanks anything that looks like a key, and
        sends them encrypted. After the deadline you and the judges see how much of your final code was already on
        your devices, shown beside the old code; you can explain each match. The code is deleted after the event.
      </p>
      {view.devices.length > 0 ? (
        <ul className="space-y-2">
          {view.devices.map((device) => (
            <Device key={device.id} device={device} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-neutral-600">No device yet.</p>
      )}
      {created && <Created created={created} />}
      {!isPast(view.deadline_at, now) && (
        <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
          <Field
            className="min-w-48 flex-1"
            label="Add a device"
            hint="One per member's laptop or desktop, for example Asha's laptop."
            value={label}
            onChange={setLabel}
            max={60}
          />
          <Button type="submit" busy={add.isPending} disabled={!view.can_add || !label.trim() || label.length > 60}>
            Add device
          </Button>
        </form>
      )}
      <ErrorNotice error={add.error} />
      {view.devices.some((d) => d.receipt_hash) && (
        <p className="text-xs text-neutral-500">
          Each scanned device's receipt hash: {view.devices.filter((d) => d.receipt_hash).map((d) => (
            <span key={d.id} className="mr-2">
              {d.label} <Mono>{d.receipt_hash}</Mono>
            </span>
          ))}
        </p>
      )}
    </Section>
  );
}
