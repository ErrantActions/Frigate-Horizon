import { useState } from "react";
import axios from "axios";
import Form from "@rjsf/shadcn";
import validator from "@rjsf/validator-ajv8";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { onvifPath, useOnvif, useOnvifMutation } from "@/hooks/use-onvif";
import OnvifFirmwarePanel from "./OnvifFirmwarePanel";

type Operation = {
  name: string;
  effect: "read" | "mutating";
  typed_confirmation: boolean;
  schema: RJSFSchema;
  readiness: string;
  device_support: "unverified";
  binary_input: boolean;
  binary_output: boolean;
  binding_id?: string;
};
type Catalog = {
  revision: string;
  complete: boolean;
  coverage?: { operation_states: Record<string, number> };
  services: {
    namespace: string;
    service: string | null;
    advertised: boolean;
    binding: string;
    operations: Operation[];
    version?: { major: number; minor: number } | null;
  }[];
};
type OperationResult = {
  state: "read" | "acknowledged_unverified" | "unsupported";
  result: unknown;
};

function privateUi(schema: RJSFSchema): UiSchema {
  const ui: UiSchema = { "ui:autocomplete": "off" };
  if (schema.format === "password") {
    ui["ui:widget"] = "password";
    ui["ui:autocomplete"] = "new-password";
  }
  if (schema.format === "data-url") ui["ui:widget"] = "file";
  for (const [name, child] of Object.entries(schema.properties ?? {})) {
    if (typeof child === "object") ui[name] = privateUi(child);
  }
  if (
    schema.type === "array" &&
    schema.items &&
    !Array.isArray(schema.items) &&
    typeof schema.items === "object"
  ) {
    ui.items = privateUi(schema.items);
  }
  return ui;
}

export default function OnvifAdministrationPanel({
  camera,
}: {
  camera: string;
}) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [enabled, setEnabled] = useState(false);
  const [selection, setSelection] = useState("");
  const [filter, setFilter] = useState("");
  const [parameters, setParameters] = useState<Record<string, unknown>>({});
  const [confirmed, setConfirmed] = useState(false);
  const [typed, setTyped] = useState("");
  const [invalid, setInvalid] = useState(false);
  const [result, setResult] = useState<OperationResult>();
  const [backupConsent, setBackupConsent] = useState(false);
  const [backupBusy, setBackupBusy] = useState(false);
  const [firmwareBusy, setFirmwareBusy] = useState(false);
  const [backupFailed, setBackupFailed] = useState(false);
  const [restoreFile, setRestoreFile] = useState<File>();
  const [restoreConsent, setRestoreConsent] = useState(false);
  const [restoreTyped, setRestoreTyped] = useState("");
  const [restoreInputKey, setRestoreInputKey] = useState(0);
  const resource = useOnvif<Catalog>(
    camera,
    "administration",
    enabled && isAdmin,
  );
  const mutation = useOnvifMutation();
  if (!isAdmin) return null;
  const operations =
    resource.data?.services.flatMap((service) =>
      service.operations.map((operation) => ({
        ...operation,
        service: service.service,
        namespace: service.namespace,
      })),
    ) ?? [];
  const selected = operations.find(
    (operation) =>
      `${operation.service}/${operation.binding_id ?? ""}/${operation.name}` ===
      selection,
  );
  const canDownloadBackup = operations.some(
    (operation) =>
      operation.service === "devicemgmt" &&
      operation.name === "GetSystemBackup" &&
      operation.readiness !== "service_reference_required",
  );
  const canRestoreBackup = operations.some(
    (operation) =>
      operation.service === "devicemgmt" &&
      operation.name === "RestoreSystem" &&
      operation.readiness !== "service_reference_required",
  );
  const restoreBackup = async () => {
    if (
      !resource.data ||
      !restoreFile ||
      !restoreConsent ||
      restoreTyped !== "RestoreSystem" ||
      restoreFile.size > 33 * 1024 * 1024
    )
      return;
    const body = new FormData();
    body.append("backup", restoreFile);
    body.append("catalog_revision", resource.data.revision);
    body.append("typed_confirmation", restoreTyped);
    body.append("confirm_device_change", "true");
    const response = await mutation.run<OperationResult>(
      "post",
      onvifPath(camera, "administration/restore"),
      body,
    );
    setRestoreFile(undefined);
    setRestoreConsent(false);
    setRestoreTyped("");
    setRestoreInputKey((value) => value + 1);
    if (response) setResult(response);
  };
  const downloadBackup = async () => {
    if (!resource.data || !backupConsent || backupBusy) return;
    setBackupBusy(true);
    setBackupFailed(false);
    try {
      const response = await axios.post<Blob>(
        onvifPath(camera, "administration/backup"),
        {
          catalog_revision: resource.data.revision,
          confirm_sensitive_download: true,
        },
        { responseType: "blob", timeout: 45000 },
      );
      const url = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = url;
      link.download = "camera-backup.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setBackupFailed(true);
    } finally {
      setBackupConsent(false);
      setBackupBusy(false);
    }
  };
  const visibleOperations = operations.filter((operation) =>
    `${operation.service} ${operation.name}`
      .toLowerCase()
      .includes(filter.trim().toLowerCase()),
  );
  const reset = () => {
    setParameters({});
    setConfirmed(false);
    setTyped("");
    setInvalid(false);
    setResult(undefined);
  };
  const execute = async (values = parameters) => {
    if (!selected?.service || selected.readiness !== "ready") return;
    if (
      selected.effect === "mutating" &&
      (!confirmed || typed !== selected.name)
    )
      return;
    const response = await mutation.run<OperationResult>(
      "post",
      onvifPath(
        camera,
        `administration/${encodeURIComponent(selected.service)}/${encodeURIComponent(selected.name)}`,
      ),
      {
        catalog_revision: resource.data?.revision,
        parameters: values,
        confirm_device_change: confirmed,
        typed_confirmation: typed,
      },
    );
    // Clear write-only credentials and files after every attempt, including failures.
    reset();
    if (response) setResult(response);
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <fieldset disabled={firmwareBusy} className="contents">
        <h3 className="text-lg font-medium">{t("administration.title")}</h3>
        <p className="text-sm">{t("administration.description")}</p>
        <Button
          variant="secondary"
          disabled={resource.isValidating || mutation.busy}
          onClick={() => {
            reset();
            setSelection("");
            if (enabled) void resource.mutate();
            else setEnabled(true);
          }}
        >
          {t("administration.load")}
        </Button>
        {resource.error && <p role="alert">{t("readFailed")}</p>}
        {resource.data && (
          <>
            {canDownloadBackup && (
              <div className="space-y-3 rounded border p-3">
                <p className="text-sm">{t("administration.backupWarning")}</p>
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={backupConsent}
                    disabled={backupBusy || mutation.busy}
                    onChange={(event) => setBackupConsent(event.target.checked)}
                  />
                  {t("administration.backupConsent")}
                </label>
                <Button
                  disabled={!backupConsent || backupBusy || mutation.busy}
                  onClick={() => void downloadBackup()}
                >
                  {t("administration.downloadBackup")}
                </Button>
                {backupFailed && (
                  <p role="alert">{t("administration.backupFailed")}</p>
                )}
              </div>
            )}
            <OnvifFirmwarePanel
              key={`${camera}/${resource.data.revision}`}
              camera={camera}
              revision={resource.data.revision}
              methods={operations
                .filter(
                  (operation) =>
                    operation.service === "devicemgmt" &&
                    operation.readiness !== "service_reference_required" &&
                    (operation.name === "UpgradeSystemFirmware" ||
                      operation.name === "StartFirmwareUpgrade"),
                )
                .map((operation) => operation.name)}
              disabled={mutation.busy || backupBusy || resource.isValidating}
              onBusyChange={setFirmwareBusy}
            />
            {canRestoreBackup && (
              <div className="space-y-3 rounded border p-3">
                <p className="text-sm">{t("administration.restoreWarning")}</p>
                <label className="flex flex-col gap-1 text-sm">
                  {t("administration.restoreFile")}
                  <Input
                    key={restoreInputKey}
                    type="file"
                    accept=".zip,application/zip"
                    disabled={mutation.busy || backupBusy}
                    onChange={(event) => {
                      setRestoreFile(event.target.files?.[0]);
                      setRestoreConsent(false);
                      setRestoreTyped("");
                    }}
                  />
                </label>
                {restoreFile && restoreFile.size > 33 * 1024 * 1024 && (
                  <p role="alert">{t("administration.restoreTooLarge")}</p>
                )}
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={restoreConsent}
                    disabled={mutation.busy || backupBusy}
                    onChange={(event) =>
                      setRestoreConsent(event.target.checked)
                    }
                  />
                  {t("administration.restoreConsent")}
                </label>
                <label className="flex flex-col gap-1 text-sm">
                  {t("administration.typeOperation", {
                    operation: "RestoreSystem",
                  })}
                  <Input
                    autoComplete="off"
                    value={restoreTyped}
                    disabled={mutation.busy || backupBusy}
                    onChange={(event) => setRestoreTyped(event.target.value)}
                  />
                </label>
                <Button
                  disabled={
                    !restoreFile ||
                    restoreFile.size > 33 * 1024 * 1024 ||
                    !restoreConsent ||
                    restoreTyped !== "RestoreSystem" ||
                    mutation.busy ||
                    backupBusy
                  }
                  onClick={() => void restoreBackup()}
                >
                  {t("administration.restoreBackup")}
                </Button>
              </div>
            )}
            <p role="status">
              {t(
                resource.data.complete
                  ? "administration.catalogReady"
                  : "administration.catalogPartial",
              )}
            </p>
            {resource.data.coverage && (
              <p className="text-sm">
                {t("administration.coverage", {
                  ready: resource.data.coverage.operation_states.ready ?? 0,
                  managed:
                    resource.data.coverage.operation_states.managed_workflow ??
                    0,
                  files:
                    (resource.data.coverage.operation_states
                      .file_transfer_required ?? 0) +
                    (resource.data.coverage.operation_states.protected_output ??
                      0),
                  schemas:
                    resource.data.coverage.operation_states
                      .schema_unavailable ?? 0,
                  references:
                    resource.data.coverage.operation_states
                      .service_reference_required ?? 0,
                })}
              </p>
            )}
            <ul className="flex flex-col gap-2 text-sm">
              {resource.data.services.map((service) => (
                <li key={service.namespace} className="rounded border p-2">
                  <div className="font-medium">
                    {service.service ?? service.namespace}
                  </div>
                  <div className="break-all">{service.namespace}</div>
                  {service.version && (
                    <div>{t("administration.version", service.version)}</div>
                  )}
                  <div>
                    {service.binding === "available"
                      ? t("administration.bindingAvailable", {
                          count: service.operations.length,
                        })
                      : t(
                          service.binding === "endpoint_rejected"
                            ? "administration.endpointRejected"
                            : "administration.bindingUnavailable",
                        )}
                  </div>
                </li>
              ))}
            </ul>
            <label className="flex flex-col gap-1">
              {t("administration.filter")}
              <Input
                value={filter}
                disabled={mutation.busy}
                onChange={(event) => {
                  reset();
                  setSelection("");
                  setFilter(event.target.value);
                }}
              />
            </label>
            <label className="flex flex-col gap-1">
              {t("administration.operation")}
              <select
                aria-label={t("administration.operation")}
                className="rounded border bg-background p-2"
                value={selection}
                disabled={mutation.busy}
                onChange={(event) => {
                  reset();
                  setSelection(event.target.value);
                }}
              >
                <option value="">{t("administration.choose")}</option>
                {visibleOperations.map((operation) => (
                  <option
                    key={`${operation.service}/${operation.binding_id ?? ""}/${operation.name}`}
                    value={`${operation.service}/${operation.binding_id ?? ""}/${operation.name}`}
                  >
                    {operation.service}: {operation.name} (
                    {t(`administration.effects.${operation.effect}`)})
                    {operation.readiness === "service_reference_required"
                      ? ` [${operation.binding_id}]`
                      : ""}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
        {selected && (
          <>
            <p className="text-sm">{t("administration.unverifiedSupport")}</p>
            <p role="status">
              {t(`administration.readiness.${selected.readiness}`)}
            </p>
            <details className="rounded border p-3">
              <summary>{t("administration.schema")}</summary>
              <pre className="max-h-96 overflow-auto pt-3 text-xs">
                {JSON.stringify(selected.schema, null, 2)}
              </pre>
            </details>
            {selected.readiness === "ready" && (
              <Form
                key={selection}
                schema={selected.schema}
                uiSchema={privateUi(selected.schema)}
                validator={validator}
                autoComplete="off"
                formData={parameters}
                disabled={mutation.busy}
                omitExtraData
                liveOmit
                showErrorList={false}
                noHtml5Validate
                onChange={(event) => {
                  setParameters(event.formData ?? {});
                  setConfirmed(false);
                  setTyped("");
                  setInvalid(false);
                }}
                onError={() => setInvalid(true)}
                onSubmit={(event) => void execute(event.formData ?? {})}
              >
                {selected.effect === "mutating" && (
                  <div className="my-3 flex flex-col gap-3">
                    <p className="text-sm">{t("administration.disruptive")}</p>
                    <label className="flex items-start gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={confirmed}
                        disabled={mutation.busy}
                        onChange={(event) => setConfirmed(event.target.checked)}
                      />
                      {t("administration.confirm")}
                    </label>
                    <label className="flex flex-col gap-1">
                      {t("administration.typeOperation", {
                        operation: selected.name,
                      })}
                      <Input
                        autoComplete="off"
                        value={typed}
                        disabled={mutation.busy}
                        onChange={(event) => setTyped(event.target.value)}
                      />
                    </label>
                  </div>
                )}
                <Button
                  type="submit"
                  disabled={
                    mutation.busy ||
                    (selected.effect === "mutating" &&
                      (!confirmed || typed !== selected.name))
                  }
                >
                  {t(
                    selected.effect === "read"
                      ? "administration.read"
                      : "administration.apply",
                  )}
                </Button>
              </Form>
            )}
            {invalid && <p role="alert">{t("administration.invalid")}</p>}
          </>
        )}
        {result && (
          <>
            <p role="status">
              {t(
                result.state === "unsupported"
                  ? "administration.unsupportedOperation"
                  : result.state === "read"
                    ? "administration.readComplete"
                    : "administration.unverified",
              )}
            </p>
            <pre className="max-h-96 overflow-auto rounded border p-3 text-xs">
              {JSON.stringify(result.result, null, 2)}
            </pre>
          </>
        )}
        {mutation.failed && (
          <p role="alert">
            {t(mutation.uncertain ? "unverified" : "writeFailed")}
          </p>
        )}
      </fieldset>
    </section>
  );
}
