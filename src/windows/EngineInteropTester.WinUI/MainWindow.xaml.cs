using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;

namespace EngineInteropTester.WinUI;

public sealed partial class MainWindow : Window
{
    private HttpClient? _httpClient;
    private string? _httpClientCacheKey;
    private readonly DispatcherQueue _dispatcherQueue;
    private DispatcherQueueTimer? _refreshTimer;
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web) { WriteIndented = true };

    public MainWindow()
    {
        InitializeComponent();
        _dispatcherQueue = DispatcherQueue.GetForCurrentThread();
    }

    private async void RefreshButton_OnClick(object sender, RoutedEventArgs e)
    {
        await RefreshPeersAsync();
    }

    private async void ExchangeBootstrapButton_OnClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var bootstrap = BootstrapTokenText.Text.Trim();
            if (string.IsNullOrWhiteSpace(bootstrap))
            {
                Log("bootstrap token is required");
                return;
            }

            var json = await PostJsonAsync("/v1/auth/bootstrap/exchange", new { bootstrapToken = bootstrap }, includeAuth: false);
            if (json.TryGetProperty("accessToken", out var tokenNode))
            {
                BearerTokenText.Text = tokenNode.GetString() ?? string.Empty;
                Log("bootstrap exchange succeeded");
            }
            else
            {
                Log("bootstrap exchange response missing accessToken");
            }
        }
        catch (Exception ex)
        {
            Log($"bootstrap exchange failed: {ex.Message}");
        }
    }

    private async void IssueDevTokenButton_OnClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var issue = await PostJsonAsync("/v1/auth/bootstrap/dev/issue", new { principal = "developer" }, includeAuth: false);
            var bootstrap = issue.TryGetProperty("bootstrapToken", out var bootstrapNode) ? bootstrapNode.GetString() ?? string.Empty : string.Empty;
            BootstrapTokenText.Text = bootstrap;
            Log("dev bootstrap token issued");
        }
        catch (Exception ex)
        {
            Log($"dev token issue failed: {ex.Message}");
        }
    }

    private async void RequestAuthorityButton_OnClick(object sender, RoutedEventArgs e)
    {
        await PostClockAsync($"/v1/interop/peers/{Uri.EscapeDataString(PeerIdText.Text.Trim())}/clock/authority/request", new
        {
            executionSessionId = SessionIdText.Text.Trim(),
            autoAccept = false
        }, "clock authority request sent");
    }

    private async void ReleaseAuthorityButton_OnClick(object sender, RoutedEventArgs e)
    {
        await PostClockAsync($"/v1/interop/peers/{Uri.EscapeDataString(PeerIdText.Text.Trim())}/clock/authority/release", new
        {
            executionSessionId = SessionIdText.Text.Trim(),
            reasonCode = "tester_release"
        }, "clock authority release sent");
    }

    private async void ApplyClockSyncButton_OnClick(object sender, RoutedEventArgs e)
    {
        var nowNs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() * 1_000_000L;
        await PostClockAsync($"/v1/interop/peers/{Uri.EscapeDataString(PeerIdText.Text.Trim())}/clock/sync/apply", new
        {
            executionSessionId = SessionIdText.Text.Trim(),
            externalTick = 120L,
            externalTimeNs = nowNs,
            reasonCode = "tester_sync"
        }, "clock sync apply sent");
    }

    private async void ExportSnapshotButton_OnClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var outputDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "EngineInteropTester", "exports");
            Directory.CreateDirectory(outputDirectory);
            var file = Path.Combine(outputDirectory, $"interop_snapshot_{DateTimeOffset.UtcNow:yyyyMMdd_HHmmss}.json");
            var payload = new
            {
                baseUrl = BaseUrlText.Text.Trim(),
                capabilityFilter = CapabilityFilterText.Text.Trim(),
                cluster = TryParseJson(PeersJson.Text),
                log = LogText.Text,
                exportedAtUtc = DateTimeOffset.UtcNow
            };
            var json = JsonSerializer.Serialize(payload, Json);
            await File.WriteAllTextAsync(file, json);
            Log($"snapshot exported: {file}");
        }
        catch (Exception ex)
        {
            Log($"export failed: {ex.Message}");
        }
    }

    private async void AutoRefreshCheck_OnChecked(object sender, RoutedEventArgs e)
    {
        if (!int.TryParse(RefreshIntervalMsText.Text.Trim(), out var intervalMs))
        {
            intervalMs = 3000;
            RefreshIntervalMsText.Text = "3000";
        }

        _refreshTimer ??= _dispatcherQueue.CreateTimer();
        _refreshTimer.Interval = TimeSpan.FromMilliseconds(Math.Clamp(intervalMs, 1000, 60000));
        _refreshTimer.Tick -= RefreshTimer_OnTick;
        _refreshTimer.Tick += RefreshTimer_OnTick;
        _refreshTimer.Start();
        Log("auto refresh enabled");
        await RefreshPeersAsync();
    }

    private void AutoRefreshCheck_OnUnchecked(object sender, RoutedEventArgs e)
    {
        _refreshTimer?.Stop();
        Log("auto refresh disabled");
    }

    private async void RefreshTimer_OnTick(DispatcherQueueTimer sender, object args)
    {
        await RefreshPeersAsync();
    }

    private async Task RefreshPeersAsync()
    {
        try
        {
            var payload = await SendGetAsync("/v1/interop/cluster/capabilities");
            TryHydratePeerSelection(payload);
            Log(BuildClusterSummary(payload));
            var filter = CapabilityFilterText.Text.Trim();
            if (!string.IsNullOrWhiteSpace(filter)
                && payload.TryGetProperty("items", out var items)
                && items.ValueKind == JsonValueKind.Array)
            {
                var filtered = items.EnumerateArray()
                    .Where(item => item.TryGetProperty("capabilities", out var caps)
                        && caps.ValueKind == JsonValueKind.Array
                        && caps.EnumerateArray().Any(cap => string.Equals(cap.GetString(), filter, StringComparison.OrdinalIgnoreCase)))
                    .ToArray();
                var projected = new
                {
                    localPeerId = payload.TryGetProperty("localPeerId", out var localPeerNode) ? localPeerNode.GetString() : null,
                    totalPeers = payload.TryGetProperty("totalPeers", out var totalNode) ? totalNode.GetInt32() : filtered.Length,
                    filteredPeers = filtered.Length,
                    capabilityFilter = filter,
                    items = filtered,
                    capturedAtUtc = DateTimeOffset.UtcNow
                };
                PeersJson.Text = JsonSerializer.Serialize(projected, Json);
            }
            else
            {
                PeersJson.Text = JsonSerializer.Serialize(payload, Json);
            }

            Log("cluster capabilities refreshed");
        }
        catch (Exception ex)
        {
            Log($"refresh failed: {ex.Message}");
        }
    }

    private async Task<JsonElement> SendGetAsync(string relativePath)
    {
        var client = GetHttpClient();
        using var request = new HttpRequestMessage(HttpMethod.Get, relativePath);
        ApplyAuthHeader(request);
        using var response = await client.SendAsync(request);
        var body = await response.Content.ReadAsStringAsync();
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException($"HTTP {(int)response.StatusCode}: {body}");
        }

        return JsonSerializer.Deserialize<JsonElement>(body, Json);
    }

    private async Task<JsonElement> PostJsonAsync(string relativePath, object payload, bool includeAuth)
    {
        var client = GetHttpClient();
        using var request = new HttpRequestMessage(HttpMethod.Post, relativePath)
        {
            Content = JsonContent.Create(payload)
        };
        if (includeAuth)
        {
            ApplyAuthHeader(request);
        }

        using var response = await client.SendAsync(request);
        var body = await response.Content.ReadAsStringAsync();
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException($"HTTP {(int)response.StatusCode}: {body}");
        }

        return JsonSerializer.Deserialize<JsonElement>(body, Json);
    }

    private async Task PostClockAsync(string relativePath, object payload, string success)
    {
        try
        {
            var json = await PostJsonAsync(relativePath, payload, includeAuth: true);
            Log(success);
            Log(JsonSerializer.Serialize(json, Json));
        }
        catch (Exception ex)
        {
            Log($"request failed: {ex.Message}");
        }
    }

    private HttpClient GetHttpClient()
    {
        var baseUrl = BaseUrlText.Text.Trim();
        var pin = NormalizeHex(TlsPinText.Text);
        var cacheKey = $"{baseUrl}|{pin}";

        if (_httpClient is not null && string.Equals(_httpClientCacheKey, cacheKey, StringComparison.Ordinal))
        {
            return _httpClient;
        }

        var baseUri = new System.Uri(baseUrl, System.UriKind.Absolute);
        var handler = new HttpClientHandler();
        if (string.Equals(baseUri.Scheme, System.Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase))
        {
            var expectedPin = NormalizeHex(TlsPinText.Text);
            if (string.IsNullOrWhiteSpace(expectedPin))
            {
                throw new InvalidOperationException("HTTPS requires TLS pin in this tester.");
            }

            handler.ServerCertificateCustomValidationCallback = (_, certificate, _, _) =>
            {
                if (certificate is null)
                {
                    return false;
                }

                var cert = certificate is X509Certificate2 cert2 ? cert2 : new X509Certificate2(certificate);
                var presented = NormalizeHex(cert.GetCertHashString(System.Security.Cryptography.HashAlgorithmName.SHA256));
                return string.Equals(expectedPin, presented, StringComparison.Ordinal);
            };
        }

        _httpClient?.Dispose();
        _httpClient = new HttpClient(handler)
        {
            BaseAddress = baseUri,
            Timeout = TimeSpan.FromSeconds(15)
        };
        _httpClientCacheKey = cacheKey;
        return _httpClient;
    }

    private void ApplyAuthHeader(HttpRequestMessage request)
    {
        var token = BearerTokenText.Text.Trim();
        if (!string.IsNullOrWhiteSpace(token))
        {
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        }
    }

    private static string NormalizeHex(string value)
        => new string((value ?? string.Empty)
            .Where(static ch => System.Uri.IsHexDigit(ch))
            .Select(static ch => char.ToUpperInvariant(ch))
            .ToArray());

    private static JsonElement? TryParseJson(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
        {
            return null;
        }

        try
        {
            return JsonSerializer.Deserialize<JsonElement>(raw, Json);
        }
        catch
        {
            return null;
        }
    }

    private void Log(string message)
    {
        LogText.Text = $"[{DateTimeOffset.Now:HH:mm:ss}] {message}\n{LogText.Text}";
    }

    private void TryHydratePeerSelection(JsonElement payload)
    {
        if (string.IsNullOrWhiteSpace(PeerIdText.Text)
            && payload.TryGetProperty("items", out var items)
            && items.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in items.EnumerateArray())
            {
                if (item.TryGetProperty("peerId", out var peerIdNode))
                {
                    var peerId = peerIdNode.GetString();
                    if (!string.IsNullOrWhiteSpace(peerId))
                    {
                        PeerIdText.Text = peerId;
                        break;
                    }
                }
            }
        }
    }

    private static string BuildClusterSummary(JsonElement payload)
    {
        if (!payload.TryGetProperty("items", out var items) || items.ValueKind != JsonValueKind.Array)
        {
            return "cluster summary unavailable";
        }

        var peerCount = 0;
        var capabilitySet = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var item in items.EnumerateArray())
        {
            peerCount++;
            if (item.TryGetProperty("capabilities", out var caps) && caps.ValueKind == JsonValueKind.Array)
            {
                foreach (var cap in caps.EnumerateArray())
                {
                    var value = cap.GetString();
                    if (!string.IsNullOrWhiteSpace(value))
                    {
                        capabilitySet.Add(value);
                    }
                }
            }
        }

        return $"cluster summary: peers={peerCount}, uniqueCapabilities={capabilitySet.Count}";
    }
}
