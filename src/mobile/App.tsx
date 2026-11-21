import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar as RNStatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { Accelerometer, Gyroscope } from 'expo-sensors';
import * as Location from 'expo-location';
import { activateKeepAwakeAsync, deactivateKeepAwake } from 'expo-keep-awake';

const DEFAULT_ENDPOINT =
    'https://u90us4gdu6.execute-api.eu-central-1.amazonaws.com/telemetry';
const SAMPLE_INTERVAL_MS = 50; // ~20 Hz
const FLUSH_INTERVAL_MS = 2000; // 2 seconds
const UI_REFRESH_INTERVAL_MS = 200; // 5 Hz UI readout update
const GRAVITY_MS2 = 9.81;
const MAX_RETRY_QUEUE = 20;
// At 20 Hz, 200 samples retain up to 10 seconds per sensor if uploads stall.
const MAX_BUFFERED_SAMPLES = 200;
const REQUEST_TIMEOUT_MS = 10000;
const KEEP_AWAKE_TAG = 'sensor-telemetry-stream';

interface SensorSample {
  t: number;
  x: number;
  y: number;
  z: number;
}

interface LocationReading {
  lat: number;
  lon: number;
  accuracy: number | null;
}

interface PackedSensorBatch {
  t0: number;
  t: number[];
  x: number[];
  y: number[];
  z: number[];
}

interface TelemetryPayload {
  schema_version: 1;
  event_type: 'raw_sensor_batch';
  device_id: string;
  platform: string;
  sent_at: number;
  timestamp: string;
  value: number;
  unit: 'm/s^2';
  gyro_unit: 'rad/s';
  sample_rate_hz: number;
  location: LocationReading | null;
  accelerometer: PackedSensorBatch;
  gyroscope: PackedSensorBatch;
}

/**
 * Packs timestamped samples into parallel arrays.
 * t contains millisecond offsets relative to t0, reducing payload size.
 */
function packSensorSamples(samples: SensorSample[], fallbackT0: number): PackedSensorBatch {
  const t0 = samples.length > 0 ? samples[0].t : fallbackT0;

  return {
    t0,
    t: samples.map((sample) => sample.t - t0),
    x: samples.map((sample) => sample.x),
    y: samples.map((sample) => sample.y),
    z: samples.map((sample) => sample.z),
  };
}



interface SubscriptionHandle {
  remove: () => void;
}

function generateDefaultDeviceId(): string {
  const randomTwoDigits = Math.floor(10 + Math.random() * 90);
  return `${Platform.OS}-${randomTwoDigits}`;
}

function roundTo(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

function formatAxis(value: number | null): string {
  if (value === null) return '--';
  return value >= 0 ? ` ${value.toFixed(2)}` : value.toFixed(2);
}

export default function App() {
  const [deviceId, setDeviceId] = useState<string>(generateDefaultDeviceId);
  const [endpoint, setEndpoint] = useState<string>(DEFAULT_ENDPOINT);
  const [isStreaming, setIsStreaming] = useState<boolean>(false);
  const [lastSentUtc, setLastSentUtc] = useState<string>('--');
  const [batchesSent, setBatchesSent] = useState<number>(0);
  const [retryQueueSize, setRetryQueueSize] = useState<number>(0);
  const [latestAccel, setLatestAccel] = useState<SensorSample | null>(null);
  const [latestGyro, setLatestGyro] = useState<SensorSample | null>(null);
  const [latestLocation, setLatestLocation] = useState<LocationReading | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Refs for high-frequency sensor buffers and active config
  const deviceIdRef = useRef<string>(deviceId);
  const endpointRef = useRef<string>(endpoint);
  const accelBufferRef = useRef<SensorSample[]>([]);
  const gyroBufferRef = useRef<SensorSample[]>([]);
  const latestAccelRef = useRef<SensorSample | null>(null);
  const latestGyroRef = useRef<SensorSample | null>(null);
  const latestLocationRef = useRef<LocationReading | null>(null);
  const retryQueueRef = useRef<TelemetryPayload[]>([]);
  const isFlushingRef = useRef<boolean>(false);
  const isStartingRef = useRef<boolean>(false);
  const isStreamingRef = useRef<boolean>(false);

  // Active subscriptions & timers
  const accelSubRef = useRef<SubscriptionHandle | null>(null);
  const gyroSubRef = useRef<SubscriptionHandle | null>(null);
  const locationSubRef = useRef<Location.LocationSubscription | null>(null);
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const uiTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    deviceIdRef.current = deviceId;
  }, [deviceId]);

  useEffect(() => {
    const timer = setInterval(() => {
      const memory = (globalThis as any).performance?.memory;

      if (memory) {
        console.log(
            `[MEMORY] Used: ${(memory.usedJSHeapSize / 1024 / 1024).toFixed(2)} MB`,
            `Total: ${(memory.totalJSHeapSize / 1024 / 1024).toFixed(2)} MB`
        );
      } else {
        console.log('[MEMORY] JS heap statistics unavailable');
      }
    }, 5000);

    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    endpointRef.current = endpoint;
  }, [endpoint]);

  const enqueueForRetry = useCallback((batches: TelemetryPayload[]) => {
    const combined = [...batches, ...retryQueueRef.current];
    // Cap at MAX_RETRY_QUEUE, keeping the most recent batches
    const trimmed =
        combined.length > MAX_RETRY_QUEUE
            ? combined.slice(combined.length - MAX_RETRY_QUEUE)
            : combined;
    retryQueueRef.current = trimmed;
    setRetryQueueSize(trimmed.length);
  }, []);

  const postBatch = useCallback(
      async (url: string, payload: TelemetryPayload): Promise<void> => {
        // A stalled request must not hold the flush lock forever. If that happened,
        // sensor callbacks would continue appending to the buffers indefinitely.
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

        let response: Response;
        try {
          response = await fetch(url, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(payload),
            signal: controller.signal,
          });
        } catch (err) {
          if (controller.signal.aborted) {
            throw new Error(`Request timed out after ${REQUEST_TIMEOUT_MS / 1000}s`);
          }
          throw err;
        } finally {
          clearTimeout(timeout);
        }

        if (!response.ok) {
          const bodyText = await response.text().catch(() => '');
          const detail = bodyText ? `: ${bodyText.slice(0, 120)}` : '';
          throw new Error(`HTTP ${response.status}${detail}`);
        }
      },
      []
  );

  const flushTelemetry = useCallback(async () => {
    if (isFlushingRef.current) {
      return;
    }

    const accelBatch = accelBufferRef.current;
    const gyroBatch = gyroBufferRef.current;
    accelBufferRef.current = [];
    gyroBufferRef.current = [];

    const pendingBatches: TelemetryPayload[] = [...retryQueueRef.current];
    retryQueueRef.current = [];

    if (accelBatch.length > 0 || gyroBatch.length > 0) {
      const lastAccelSample = accelBatch[accelBatch.length - 1];
      const magnitude = lastAccelSample
          ? roundTo(
              Math.sqrt(
                  lastAccelSample.x * lastAccelSample.x +
                  lastAccelSample.y * lastAccelSample.y +
                  lastAccelSample.z * lastAccelSample.z
              ),
              2
          )
          : 0;

      const sentAt = Date.now();

      const newPayload: TelemetryPayload = {
        schema_version: 1,
        event_type: 'raw_sensor_batch',
        device_id: deviceIdRef.current.trim() || 'unknown-device',
        platform: Platform.OS,
        sent_at: sentAt,
        timestamp: new Date(sentAt).toISOString(),
        value: magnitude,
        unit: 'm/s^2',
        gyro_unit: 'rad/s',
        sample_rate_hz: 1000 / SAMPLE_INTERVAL_MS,
        location: latestLocationRef.current,
        accelerometer: packSensorSamples(accelBatch, sentAt),
        gyroscope: packSensorSamples(gyroBatch, sentAt),
      };

      pendingBatches.push(newPayload);
    }

    if (pendingBatches.length === 0) {
      setRetryQueueSize(0);
      return;
    }

    // Cap total pending batches before attempting delivery
    const boundedBatches =
        pendingBatches.length > MAX_RETRY_QUEUE
            ? pendingBatches.slice(pendingBatches.length - MAX_RETRY_QUEUE)
            : pendingBatches;

    const targetUrl = endpointRef.current.trim();
    if (!targetUrl) {
      enqueueForRetry(boundedBatches);
      setErrorMessage('API Endpoint URL is empty.');
      return;
    }

    isFlushingRef.current = true;
    try {
      let sentCount = 0;

      for (let i = 0; i < boundedBatches.length; i += 1) {
        const batch = boundedBatches[i];
        try {
          await postBatch(targetUrl, batch);
          sentCount += 1;
        } catch (err) {
          const remaining = boundedBatches.slice(i);
          enqueueForRetry(remaining);
          const msg = err instanceof Error ? err.message : 'Network request failed';
          setErrorMessage(`Upload failed (${msg}). Queued ${retryQueueRef.current.length}/${MAX_RETRY_QUEUE} batches.`);
          if (sentCount > 0) {
            setBatchesSent((prev) => prev + sentCount);
            setLastSentUtc(new Date().toISOString());
          }
          return;
        }
      }

      if (sentCount > 0) {
        setBatchesSent((prev) => prev + sentCount);
        setLastSentUtc(new Date().toISOString());
      }
      setRetryQueueSize(retryQueueRef.current.length);
      setErrorMessage((prev) => (prev && prev.startsWith('Upload failed') ? null : prev));
    } finally {
      isFlushingRef.current = false;
    }
  }, [enqueueForRetry, postBatch]);

  const stopStreaming = useCallback(async () => {
    isStreamingRef.current = false;

    if (flushTimerRef.current) {
      clearInterval(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    if (uiTimerRef.current) {
      clearInterval(uiTimerRef.current);
      uiTimerRef.current = null;
    }
    if (accelSubRef.current) {
      accelSubRef.current.remove();
      accelSubRef.current = null;
    }
    if (gyroSubRef.current) {
      gyroSubRef.current.remove();
      gyroSubRef.current = null;
    }
    if (locationSubRef.current) {
      locationSubRef.current.remove();
      locationSubRef.current = null;
    }

    accelBufferRef.current = [];
    gyroBufferRef.current = [];
    setIsStreaming(false);

    try {
      await deactivateKeepAwake(KEEP_AWAKE_TAG);
    } catch {
      // Ignore keep-awake cleanup errors
    }
  }, []);

  const startStreaming = useCallback(async () => {
    // React state updates are asynchronous. A ref prevents rapid taps from
    // creating multiple sensor subscriptions and timers.
    if (isStartingRef.current || isStreamingRef.current) {
      return;
    }
    isStartingRef.current = true;

    try {
      setErrorMessage(null);

      if (!endpointRef.current.trim()) {
        setErrorMessage('Please enter a valid API Endpoint URL before starting.');
        return;
      }

      try {
        await activateKeepAwakeAsync(KEEP_AWAKE_TAG);
      } catch {
        // Non-fatal on unsupported environments
      }

      // Request foreground GPS permissions
      try {
        const { status } = await Location.requestForegroundPermissionsAsync();
        if (status !== 'granted') {
          setErrorMessage(
              'Foreground location permission denied. Streaming sensors without GPS coordinates.'
          );
        } else {
          try {
            const lastKnown = await Location.getLastKnownPositionAsync();
            if (lastKnown) {
              const reading: LocationReading = {
                lat: roundTo(lastKnown.coords.latitude, 6),
                lon: roundTo(lastKnown.coords.longitude, 6),
                accuracy:
                    lastKnown.coords.accuracy !== null
                        ? roundTo(lastKnown.coords.accuracy, 2)
                        : null,
              };
              latestLocationRef.current = reading;
              setLatestLocation(reading);
            }
          } catch {
            // Wait for live watchPosition updates
          }

          locationSubRef.current = await Location.watchPositionAsync(
              {
                accuracy: Location.Accuracy.Balanced,
                timeInterval: 2000,
                distanceInterval: 1,
              },
              (loc) => {
                const reading: LocationReading = {
                  lat: roundTo(loc.coords.latitude, 6),
                  lon: roundTo(loc.coords.longitude, 6),
                  accuracy:
                      loc.coords.accuracy !== null
                          ? roundTo(loc.coords.accuracy, 2)
                          : null,
                };
                latestLocationRef.current = reading;
              }
          );
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Unable to access GPS';
        setErrorMessage(`Location error: ${msg}`);
      }

      // Start Accelerometer (~20 Hz, converted from G-force to m/s^2)
      try {
        const accelAvailable = await Accelerometer.isAvailableAsync();
        if (accelAvailable) {
          Accelerometer.setUpdateInterval(SAMPLE_INTERVAL_MS);
          accelSubRef.current = Accelerometer.addListener((data) => {
            const sample: SensorSample = {
              t: Date.now(),
              x: roundTo(data.x * GRAVITY_MS2, 4),
              y: roundTo(data.y * GRAVITY_MS2, 4),
              z: roundTo(data.z * GRAVITY_MS2, 4),
            };
            latestAccelRef.current = sample;
            if (accelBufferRef.current.length >= MAX_BUFFERED_SAMPLES) {
              accelBufferRef.current.shift();
            }
            accelBufferRef.current.push(sample);
          });
        } else {
          setErrorMessage('Accelerometer is not available on this device.');
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Accelerometer error';
        setErrorMessage(`Accelerometer error: ${msg}`);
      }

      // Start Gyroscope (~20 Hz)
      try {
        const gyroAvailable = await Gyroscope.isAvailableAsync();
        if (gyroAvailable) {
          Gyroscope.setUpdateInterval(SAMPLE_INTERVAL_MS);
          gyroSubRef.current = Gyroscope.addListener((data) => {
            const sample: SensorSample = {
              t: Date.now(),
              x: roundTo(data.x, 4),
              y: roundTo(data.y, 4),
              z: roundTo(data.z, 4),
            };
            latestGyroRef.current = sample;
            if (gyroBufferRef.current.length >= MAX_BUFFERED_SAMPLES) {
              gyroBufferRef.current.shift();
            }
            gyroBufferRef.current.push(sample);
          });
        } else {
          setErrorMessage((prev) => prev ?? 'Gyroscope is not available on this device.');
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Gyroscope error';
        setErrorMessage((prev) => prev ?? `Gyroscope error: ${msg}`);
      }

      // Periodically refresh displayed telemetry without re-rendering at 20 Hz
      uiTimerRef.current = setInterval(() => {
        if (latestAccelRef.current) {
          setLatestAccel(latestAccelRef.current);
        }
        if (latestGyroRef.current) {
          setLatestGyro(latestGyroRef.current);
        }
        if (latestLocationRef.current) {
          setLatestLocation(latestLocationRef.current);
        }
      }, UI_REFRESH_INTERVAL_MS);

      // Flush batched readings every 2 seconds
      flushTimerRef.current = setInterval(() => {
        void flushTelemetry();
      }, FLUSH_INTERVAL_MS);

      isStreamingRef.current = true;
      setIsStreaming(true);
    } finally {
      isStartingRef.current = false;
    }
  }, [flushTelemetry]);

  useEffect(() => {
    return () => {
      void stopStreaming();
    };
  }, [stopStreaming]);

  const handleToggleStreaming = useCallback(() => {
    if (isStreaming) {
      void stopStreaming();
    } else {
      void startStreaming();
    }
  }, [isStreaming, startStreaming, stopStreaming]);

  const accelMagnitude = latestAccel
      ? roundTo(
          Math.sqrt(
              latestAccel.x * latestAccel.x +
              latestAccel.y * latestAccel.y +
              latestAccel.z * latestAccel.z
          ),
          2
      ).toFixed(2)
      : '--';

  return (
      <SafeAreaView style={styles.safeArea}>
        <StatusBar style="light" />
        <KeyboardAvoidingView
            style={styles.flex}
            behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        >
          <ScrollView
              contentContainerStyle={styles.scrollContent}
              keyboardShouldPersistTaps="handled"
          >
            {/* Header */}
            <View style={styles.header}>
              <Text style={styles.title}>Sensor Telemetry Streamer</Text>
              <View
                  style={[
                    styles.statusBadge,
                    isStreaming ? styles.statusBadgeActive : styles.statusBadgeIdle,
                  ]}
              >
                <View
                    style={[
                      styles.statusDot,
                      isStreaming ? styles.statusDotActive : styles.statusDotIdle,
                    ]}
                />
                <Text style={styles.statusBadgeText}>
                  {isStreaming ? 'STREAMING' : 'IDLE'}
                </Text>
              </View>
            </View>

            {/* Error Banner */}
            {errorMessage && (
                <View style={styles.errorBanner}>
                  <Text style={styles.errorTitle}>Warning / Error</Text>
                  <Text style={styles.errorText}>{errorMessage}</Text>
                </View>
            )}

            {/* Configuration Card */}
            <View style={styles.card}>
              <Text style={styles.sectionTitle}>Configuration</Text>

              <Text style={styles.inputLabel}>Device ID</Text>
              <TextInput
                  style={styles.input}
                  value={deviceId}
                  onChangeText={setDeviceId}
                  placeholder="e.g. ios-42"
                  placeholderTextColor="#64748B"
                  autoCapitalize="none"
                  autoCorrect={false}
              />

              <Text style={styles.inputLabel}>API Endpoint</Text>
              <TextInput
                  style={styles.input}
                  value={endpoint}
                  onChangeText={setEndpoint}
                  placeholder="https://..."
                  placeholderTextColor="#64748B"
                  autoCapitalize="none"
                  autoCorrect={false}
                  keyboardType="url"
              />
            </View>

            {/* Primary Action Button */}
            <Pressable
                onPress={handleToggleStreaming}
                style={({ pressed }) => [
                  styles.actionButton,
                  isStreaming ? styles.stopButton : styles.startButton,
                  pressed && styles.actionButtonPressed,
                ]}
            >
              <Text style={styles.actionButtonText}>
                {isStreaming ? 'Stop Streaming' : 'Start Streaming'}
              </Text>
            </Pressable>

            {/* Transmission Stats Card */}
            <View style={styles.card}>
              <Text style={styles.sectionTitle}>Transmission Status</Text>

              <View style={styles.row}>
                <Text style={styles.metricLabel}>Status</Text>
                <Text
                    style={[
                      styles.metricValue,
                      isStreaming ? styles.textGreen : styles.textMuted,
                    ]}
                >
                  {isStreaming ? 'Streaming (20 Hz / 2s batch)' : 'Stopped'}
                </Text>
              </View>

              <View style={styles.row}>
                <Text style={styles.metricLabel}>Last Sent (UTC)</Text>
                <Text style={styles.metricMono}>{lastSentUtc}</Text>
              </View>

              <View style={styles.row}>
                <Text style={styles.metricLabel}>Batches Sent</Text>
                <Text style={styles.metricValue}>{batchesSent}</Text>
              </View>

              <View style={[styles.row, styles.rowLast]}>
                <Text style={styles.metricLabel}>Retry Queue</Text>
                <Text
                    style={[
                      styles.metricValue,
                      retryQueueSize > 0 ? styles.textAmber : styles.textPrimary,
                    ]}
                >
                  {retryQueueSize} / {MAX_RETRY_QUEUE}
                </Text>
              </View>
            </View>

            {/* Live Sensor Readout Card */}
            <View style={styles.card}>
              <Text style={styles.sectionTitle}>Live Sensor Readout</Text>

              <View style={styles.sensorBlock}>
                <View style={styles.sensorHeader}>
                  <Text style={styles.sensorName}>Accelerometer (m/s²)</Text>
                  <Text style={styles.sensorMeta}>|a| = {accelMagnitude}</Text>
                </View>
                <View style={styles.axisRow}>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>X</Text>
                    <Text style={styles.axisValue}>
                      {formatAxis(latestAccel ? latestAccel.x : null)}
                    </Text>
                  </View>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>Y</Text>
                    <Text style={styles.axisValue}>
                      {formatAxis(latestAccel ? latestAccel.y : null)}
                    </Text>
                  </View>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>Z</Text>
                    <Text style={styles.axisValue}>
                      {formatAxis(latestAccel ? latestAccel.z : null)}
                    </Text>
                  </View>
                </View>
              </View>

              <View style={styles.sensorBlock}>
                <View style={styles.sensorHeader}>
                  <Text style={styles.sensorName}>Gyroscope (rad/s)</Text>
                </View>
                <View style={styles.axisRow}>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>X</Text>
                    <Text style={styles.axisValue}>
                      {formatAxis(latestGyro ? latestGyro.x : null)}
                    </Text>
                  </View>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>Y</Text>
                    <Text style={styles.axisValue}>
                      {formatAxis(latestGyro ? latestGyro.y : null)}
                    </Text>
                  </View>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>Z</Text>
                    <Text style={styles.axisValue}>
                      {formatAxis(latestGyro ? latestGyro.z : null)}
                    </Text>
                  </View>
                </View>
              </View>

              <View style={[styles.sensorBlock, styles.sensorBlockLast]}>
                <View style={styles.sensorHeader}>
                  <Text style={styles.sensorName}>GPS Location</Text>
                  <Text style={styles.sensorMeta}>
                    {latestLocation?.accuracy != null
                        ? `±${latestLocation.accuracy}m`
                        : 'No fix'}
                  </Text>
                </View>
                <View style={styles.axisRow}>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>LAT</Text>
                    <Text style={styles.axisValue}>
                      {latestLocation ? latestLocation.lat.toFixed(5) : '--'}
                    </Text>
                  </View>
                  <View style={styles.axisBox}>
                    <Text style={styles.axisLabel}>LON</Text>
                    <Text style={styles.axisValue}>
                      {latestLocation ? latestLocation.lon.toFixed(5) : '--'}
                    </Text>
                  </View>
                </View>
              </View>
            </View>
          </ScrollView>
        </KeyboardAvoidingView>
      </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: '#0B1120',
    paddingTop: Platform.OS === 'android' ? RNStatusBar.currentHeight ?? 0 : 0,
  },
  flex: {
    flex: 1,
  },
  scrollContent: {
    padding: 16,
    paddingBottom: 32,
    gap: 14,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 4,
  },
  title: {
    color: '#F8FAFC',
    fontSize: 20,
    fontWeight: '700',
    letterSpacing: 0.2,
  },
  statusBadge: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 999,
    borderWidth: 1,
    gap: 6,
  },
  statusBadgeActive: {
    backgroundColor: 'rgba(34, 197, 94, 0.14)',
    borderColor: '#22C55E',
  },
  statusBadgeIdle: {
    backgroundColor: 'rgba(148, 163, 184, 0.12)',
    borderColor: '#475569',
  },
  statusDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  statusDotActive: {
    backgroundColor: '#22C55E',
  },
  statusDotIdle: {
    backgroundColor: '#94A3B8',
  },
  statusBadgeText: {
    color: '#F8FAFC',
    fontSize: 11,
    fontWeight: '700',
    letterSpacing: 0.8,
  },
  errorBanner: {
    backgroundColor: 'rgba(239, 68, 68, 0.15)',
    borderColor: '#EF4444',
    borderWidth: 1,
    borderRadius: 12,
    padding: 12,
    gap: 4,
  },
  errorTitle: {
    color: '#FCA5A5',
    fontSize: 12,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.6,
  },
  errorText: {
    color: '#FEE2E2',
    fontSize: 13,
    lineHeight: 18,
  },
  card: {
    backgroundColor: '#111827',
    borderColor: '#1F2937',
    borderWidth: 1,
    borderRadius: 14,
    padding: 14,
    gap: 10,
  },
  sectionTitle: {
    color: '#94A3B8',
    fontSize: 12,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.8,
    marginBottom: 2,
  },
  inputLabel: {
    color: '#CBD5E1',
    fontSize: 13,
    fontWeight: '600',
  },
  input: {
    backgroundColor: '#0F172A',
    borderColor: '#334155',
    borderWidth: 1,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10,
    color: '#F8FAFC',
    fontSize: 14,
    fontFamily: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
  },
  actionButton: {
    borderRadius: 14,
    paddingVertical: 18,
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: '#000',
    shadowOpacity: 0.3,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 4 },
    elevation: 4,
  },
  startButton: {
    backgroundColor: '#16A34A',
  },
  stopButton: {
    backgroundColor: '#DC2626',
  },
  actionButtonPressed: {
    opacity: 0.85,
  },
  actionButtonText: {
    color: '#FFFFFF',
    fontSize: 18,
    fontWeight: '700',
    letterSpacing: 0.4,
  },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingVertical: 6,
    borderBottomColor: '#1F2937',
    borderBottomWidth: 1,
    gap: 12,
  },
  rowLast: {
    borderBottomWidth: 0,
    paddingBottom: 0,
  },
  metricLabel: {
    color: '#94A3B8',
    fontSize: 13,
  },
  metricValue: {
    color: '#F8FAFC',
    fontSize: 14,
    fontWeight: '600',
  },
  metricMono: {
    color: '#E2E8F0',
    fontSize: 12,
    fontFamily: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
  },
  textGreen: {
    color: '#4ADE80',
  },
  textAmber: {
    color: '#FBBF24',
  },
  textMuted: {
    color: '#94A3B8',
  },
  textPrimary: {
    color: '#F8FAFC',
  },
  sensorBlock: {
    paddingBottom: 10,
    borderBottomColor: '#1F2937',
    borderBottomWidth: 1,
    gap: 8,
  },
  sensorBlockLast: {
    borderBottomWidth: 0,
    paddingBottom: 0,
  },
  sensorHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  sensorName: {
    color: '#E2E8F0',
    fontSize: 14,
    fontWeight: '600',
  },
  sensorMeta: {
    color: '#38BDF8',
    fontSize: 12,
    fontFamily: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
  },
  axisRow: {
    flexDirection: 'row',
    gap: 8,
  },
  axisBox: {
    flex: 1,
    backgroundColor: '#0F172A',
    borderColor: '#1E293B',
    borderWidth: 1,
    borderRadius: 10,
    paddingVertical: 8,
    paddingHorizontal: 10,
    alignItems: 'center',
    gap: 2,
  },
  axisLabel: {
    color: '#64748B',
    fontSize: 11,
    fontWeight: '700',
    letterSpacing: 0.6,
  },
  axisValue: {
    color: '#F8FAFC',
    fontSize: 14,
    fontWeight: '600',
    fontFamily: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
  },
});
