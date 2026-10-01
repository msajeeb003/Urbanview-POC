/**
 * Named aliases over the generated OpenAPI types (`schema.d.ts`, regenerated with
 * `npm run api:types` after `python -m api.export_openapi` in `backend/`). Never hand-edit the
 * generated file; add an alias here when a component needs a schema by name.
 */
import type { components, paths } from "./schema";

type S = components["schemas"];

export type MunicipalityProfile = S["MunicipalityProfile"];

export type LocationResolution = S["LocationResolution"];

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
export type PanelQuery = paths["/v1/panel"]["get"]["parameters"]["query"];


export type SourcePage = S["SourcePage"];

export type AnalyticsEventName = S["AnalyticsEvent"];
export type EventIn = S["EventIn"];
export type EventBatch = S["EventBatch"];
export type IngestResult = S["IngestResult"];

export type TilesCurrent = S["TilesCurrent"];

export type ZoneIndex = S["ZoneIndex"];
export type ZoneIndexEntry = S["ZoneIndexEntry"];

export type UrbanParcelSearch = S["UrbanParcelSearch"];
export type UrbanParcelMatch = S["UrbanParcelMatch"];
export type OrderIn = S["OrderIn"];
export type OrderCreated = S["OrderCreated"];
export type OrderPublic = S["OrderPublic"];
export type OrderPricing = S["OrderPricing"];

export type PlanningField = S["PlanningField"];
export type UrbanLink = S["UrbanLink"];

// --- the admin console (staff routes; called from the Next server with the staff session) ---
export type StaffMe = S["StaffMeOut"];
export type StaffSession = S["SessionOut"];
export type AuditPage = S["AuditPage"];
export type AnalyticsDashboard = S["AnalyticsDashboard"];
export type AuditEntry = S["AuditEntry"];
export type OrderList = S["OrderList"];
export type StaffUserList = S["StaffUserList"];

// --- Data sources: files, planning document versions and their files, pipeline jobs ---
export type AdminDocument = S["DocumentOut"];
export type AdminDocumentFile = S["DocumentFileOut"];
export type AdminDocumentList = S["DocumentList"];
export type AdminJob = S["JobOut"];
export type AdminJobList = S["JobList"];
export type AdminGeoreference = S["GeoreferenceOut"];
export type StoredFile = S["StoredFileOut"];
export type UploadResult = S["UploadResult"];
export type DocumentState = NonNullable<S["DocumentOut"]["state"]>;
export type DocumentStatus = S["DocumentOut"]["status"];
export type ExtractionState = NonNullable<S["DocumentFileOut"]["extraction_state"]>;
export type FileRole = S["DocumentFileOut"]["role"];
export type StaffRoleName = S["Role"];
export type JobStateFilter = NonNullable<
  NonNullable<paths["/v1/admin/documents"]["get"]["parameters"]["query"]>["job_state"]
>;

// --- AI review queue and publishing ---
export type ReviewItem = S["ReviewItem"];
export type ReviewPage = S["ReviewPage"];
export type ReviewValue = S["ReviewValue"];
export type ReviewTarget = S["ReviewTarget"];
export type ReviewCounters = S["ReviewCounters"];
export type ReviewOptions = S["ReviewOptions"];
export type ReviewStatus = S["ReviewItem"]["status"];
export type ReviewEntityType = S["ReviewTarget"]["entity_type"];
export type ReviewSort = NonNullable<NonNullable<paths["/v1/admin/review"]["get"]["parameters"]["query"]>["sort"]>;
export type ReviewPayload = S["ReviewPayload"];
export type PublishStatus = S["PublishStatus"];
// the geometry review (staged batches, the pilot scope's geometry_draft)
export type GeometryDraft = S["GeometryDraft"];
export type GeometryPage = S["GeometryPage"];
export type GeometryCounts = S["GeometryCounts"];
export type GeometryFeatures = S["GeometryFeatures"];
export type QaIssue = S["QaIssueOut"];
export type GeometryOrigin = NonNullable<S["GeometryDraft"]["origin"]>;
export type GeometryReviewStatus = NonNullable<S["GeometryDraft"]["review_status"]>;
export type PublishVersion = S["PublishVersionOut"];

// --- expert-analysis orders (staff) ---
export type OrderSummary = S["OrderSummary"];
export type OrderDetail = S["OrderOut"];
export type OrderEvent = S["OrderEvent"];
export type OrderStatus = S["OrderSummary"]["status"];
export type OrderExpert = S["ExpertOut"];
export type OrderEmail = NonNullable<S["OrderOut"]["emails"]>[number];

// Admin console: financial assumptions
export type AssumptionSet = S["AssumptionsOut"];
export type AssumptionSetList = S["AssumptionsList"];
export type FormulaVersion = S["FormulaVersionOut"];
export type AssumptionSetIn = S["AssumptionsIn"];
export type AssumptionStatus = S["AssumptionsOut"]["status"];
export type RateRange = S["RateRange"];
export type AssumptionsBatchOut = S["AssumptionsBatchOut"];
export type DataSource = S["DataSource"];
