/**
 * Named aliases over the generated OpenAPI types (`schema.d.ts`, regenerated with
 * `npm run api:types` after `python -m api.export_openapi` in `backend/`). Never hand-edit the
 * generated file; add an alias here when a component needs a schema by name.
 */
import type { components, paths } from "./schema";

type S = components["schemas"];

export type MunicipalityProfile = S["MunicipalityProfile"];

export type LocationResolution = S["LocationResolution"];
export type LatLng = S["LatLng"];
export type CoverageReason = S["CoverageReason"];

export type GeocodeResponse = S["GeocodeResponse"];
export type GeocodeResult = S["GeocodeResult"];
export type GeocodeKind = GeocodeResult["kind"];
/** Value of the CORS-exposed `X-Geocode-Status` header. */
export type GeocodeStatus = "hit" | "miss" | "too_short" | "throttled" | "provider_unavailable";

export type ParcelPanel = S["ParcelPanel"];
export type ZonePanel = S["ZonePanel"];
export type DocumentPanel = S["DocumentPanel"];
export type CadastralPanel = S["CadastralPanel"];
export type UrbanPanel = S["UrbanPanel"];
export type Panel = ZonePanel | DocumentPanel | CadastralPanel | UrbanPanel;
export type PanelType = paths["/v1/panel"]["get"]["parameters"]["query"]["type"];
export type PanelQuery = paths["/v1/panel"]["get"]["parameters"]["query"];

export type FeasibilityRequest = S["FeasibilityRequest"];
export type FeasibilityResponse = S["FeasibilityResponse"];
export type EditedAssumptions = S["EditedAssumptions"];

export type SourcePage = S["SourcePage"];

export type AnalyticsEventName = S["AnalyticsEvent"];
export type EventIn = S["EventIn"];
export type EventBatch = S["EventBatch"];
export type IngestResult = S["IngestResult"];

export type TilesCurrent = S["TilesCurrent"];

export type ZoneIndex = S["ZoneIndex"];
export type ZoneIndexEntry = S["ZoneIndexEntry"];

export type OrderIn = S["OrderIn"];
export type OrderCreated = S["OrderCreated"];
export type OrderStatusPublic = S["OrderStatusPublic"];
export type OrderPricing = S["OrderPricing"];

export type PlanningField = S["PlanningField"];
export type Areas = S["Areas"];
export type UrbanLink = S["UrbanLink"];
