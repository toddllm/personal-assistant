export interface ServiceDef {
  id: string;
  label: string;
  port: number;
  healthEndpoint: string | null;
  uiUrl: string;
  script?: string;
  entrypoint?: string;
  logFile: string;
  autostart: boolean;
  group: string;
}

export type HealthStatus = "green" | "yellow" | "red" | "unknown";

export interface HealthResult {
  service_id: string;
  status: HealthStatus;
  detail: string | null;
}

export interface ServiceControlResult {
  service_id: string;
  action: string;
  success: boolean;
  output: string;
}
