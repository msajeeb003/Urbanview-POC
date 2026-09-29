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
export type OrderPublic = S["OrderPublic"];
export type OrderPricing = S["OrderPricing"];

export type PlanningField = S["PlanningField"];
export type Areas = S["Areas"];
export type UrbanLink = S["UrbanLink"];

// --- the admin console (staff routes; called from the Next server with the staff session) ---
export type StaffMe = S["StaffMeOut"];
export type StaffSession = S["SessionOut"];
export type AdminOverview = S["OverviewOut"];
export type DistrictStatus = S["DistrictStatus"];
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
export type AdminVersionRef = S["VersionRef"];
export type AdminGeoreference = S["GeoreferenceOut"];
export type StoredFile = S["StoredFileOut"];
export type UploadResult = S["UploadResult"];
export type DocumentState = NonNullable<S["DocumentOut"]["state"]>;
export type DocumentStatus = S["DocumentOut"]["status"];
export type ExtractionState = NonNullable<S["DocumentFileOut"]["extraction_state"]>;
export type FileRole = S["DocumentFileOut"]["role"];
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
export type BulkResult = S["BulkResult"];
export type PublishStatus = S["PublishStatus"];
export type PublishVersion = S["PublishVersionOut"];

// --- expert-analysis orders (staff) ---
export type OrderSummary = S["OrderSummary"];
export type OrderDetail = S["OrderOut"];
export type OrderEvent = S["OrderEvent"];
export type OrderStatus = S["OrderSummary"]["status"];
export type OrderExpert = S["ExpertOut"];
export type OrderEmail = NonNullable<S["OrderOut"]["emails"]>[number];

// Admin console: financial assumptions, calculation engine, planning rules
export type AssumptionSet = S["AssumptionsOut"];
export type AssumptionSetList = S["AssumptionsList"];
export type AssumptionSetIn = S["AssumptionsIn"];
export type AssumptionStatus = S["AssumptionsOut"]["status"];
export type RateIn = S["RateIn"];
export type RateRange = S["RateRange"];
export type AssumptionsBatchOut = S["AssumptionsBatchOut"];
export type AssumptionsPreview = S["AssumptionsPreviewOut"];
export type AssumptionsPreviewIn = S["AssumptionsPreviewIn"];
export type PreviewSide = S["PreviewSide"];
export type PreviewParcel = S["PreviewParcel"];
export type PreviewParcelList = S["PreviewParcelList"];
export type Group2View = S["Group2"];
export type EngineProposal = S["EngineProposalOut"];
export type EngineProposalList = S["EngineProposalList"];
export type EngineProposalIn = S["EngineProposalIn"];
export type DataSource = S["DataSource"];
export type ZoneParameterSet = S["ZoneParametersOut"];
export type ZoneParameterList = S["ZoneParametersList"];
export type ZoneParametersIn = S["ZoneParametersIn"];
export type ZoneParametersUpdate = S["ZoneParametersUpdate"];
