import { invoke } from "@tauri-apps/api/core";
import type { ServiceDef, HealthResult } from "./services";

export async function checkHealth(service: ServiceDef): Promise<HealthResult> {
  try {
    return await invoke<HealthResult>("check_health", {
      serviceId: service.id,
      port: service.port,
      healthEndpoint: service.healthEndpoint,
    });
  } catch {
    return {
      service_id: service.id,
      status: "red",
      detail: "invoke failed",
    };
  }
}
