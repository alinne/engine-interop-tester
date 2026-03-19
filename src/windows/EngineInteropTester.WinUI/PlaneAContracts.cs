using System.Text.Json.Serialization;

namespace EngineInteropTester.WinUI;

internal static class PlaneARoutes
{
    public const string BootstrapExchange = "/v1/auth/bootstrap/exchange";
    public const string DeveloperBootstrapIssue = "/v1/auth/bootstrap/dev/issue";
    public const string ClusterCapabilities = "/v1/interop/cluster/capabilities";

    public static string RequestClockAuthority(string peerId) =>
        $"/v1/interop/peers/{Uri.EscapeDataString(peerId)}/clock/authority/request";

    public static string ReleaseClockAuthority(string peerId) =>
        $"/v1/interop/peers/{Uri.EscapeDataString(peerId)}/clock/authority/release";

    public static string RespondClockAuthority(string peerId) =>
        $"/v1/interop/peers/{Uri.EscapeDataString(peerId)}/clock/authority/respond";

    public static string GetClockAuthorityStatus(string peerId) =>
        $"/v1/interop/peers/{Uri.EscapeDataString(peerId)}/clock/authority/status";

    public static string ApplyClockSync(string peerId) =>
        $"/v1/interop/peers/{Uri.EscapeDataString(peerId)}/clock/sync/apply";
}

internal sealed record BootstrapExchangeContract(
    [property: JsonPropertyName("bootstrapToken")] string BootstrapToken,
    [property: JsonPropertyName("bootstrapMethodHint")] string? BootstrapMethodHint = null);

internal sealed record DeveloperBootstrapIssueContract(
    [property: JsonPropertyName("principal")] string Principal);

internal sealed record ClockAuthorityRequestContract(
    [property: JsonPropertyName("executionSessionId")] string ExecutionSessionId,
    [property: JsonPropertyName("autoAccept")] bool AutoAccept = false,
    [property: JsonPropertyName("localInteropSessionId")] string? LocalInteropSessionId = null,
    [property: JsonPropertyName("requestId")] string? RequestId = null,
    [property: JsonPropertyName("reasonCode")] string? ReasonCode = null);

internal sealed record ClockAuthorityReleaseContract(
    [property: JsonPropertyName("executionSessionId")] string ExecutionSessionId);

internal sealed record ClockAuthorityStatusContract(
    [property: JsonPropertyName("executionSessionId")] string ExecutionSessionId);

internal sealed record ClockAuthorityRespondContract(
    [property: JsonPropertyName("executionSessionId")] string ExecutionSessionId,
    [property: JsonPropertyName("accepted")] bool Accepted,
    [property: JsonPropertyName("reasonCode")] string? ReasonCode = null);

internal sealed record ClockSyncApplyContract(
    [property: JsonPropertyName("executionSessionId")] string ExecutionSessionId,
    [property: JsonPropertyName("externalTick")] long ExternalTick,
    [property: JsonPropertyName("externalTimeNs")] long ExternalTimeNs,
    [property: JsonPropertyName("reasonCode")] string? ReasonCode = null);
