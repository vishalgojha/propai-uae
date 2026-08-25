"use client";

import { GoogleMap, InfoWindow, Marker, useJsApiLoader } from "@react-google-maps/api";
import Link from "next/link";
import { useMemo, useState } from "react";
import type { NaturalSearchResult } from "@/lib/natural-search";
import { slugify } from "@/lib/supabase";
import { formatBuildingName } from "@/lib/listing-display";

type Props = { results: NaturalSearchResult[]; apiKey: string | null };
type Point = { lat: number; lng: number };

const containerStyle = { width: "100%", height: "100%" };
const defaultCenter: Point = { lat: 19.076, lng: 72.8777 };

function formatPrice(value: number | null, unit?: string | null): string {
  if (value == null) return "Price on request";
  const u = (unit || "").toLowerCase();
  if (u === "m" || u === "million") return `AED ${value % 1 === 0 ? value : value.toFixed(2)}M`;
  if (u === "k" || u === "thousand") return `AED ${Math.round(value)}k`;
  return `AED ${Math.round(value).toLocaleString("en-AE")}`;
}

function markerColor(result: NaturalSearchResult): string {
  return result.intent === "RENT" ? "#22c55e" : result.intent === "SELL" ? "#3b82f6" : "#6B8E63";
}

export default function SearchMap({ results, apiKey }: Props) {
  const [selected, setSelected] = useState<NaturalSearchResult | null>(null);
  const geocoded = useMemo(
    () => results.filter((result) => result.latitude != null && result.longitude != null),
    [results],
  );
  const { isLoaded, loadError } = useJsApiLoader({
    id: "propai-google-maps",
    googleMapsApiKey: apiKey || "",
  });

  if (geocoded.length === 0) {
    return (
      <div className="flex w-full h-[360px] lg:h-[480px] flex-col items-center justify-center gap-3 rounded-2xl border border-white/10 bg-zinc-900/60 px-6 text-center">
        <div className="text-2xl text-zinc-500">📍</div>
        <p className="text-sm text-zinc-400">
          No listings with coordinates yet. Showing {results.length} listings without map pins.
        </p>
        <p className="text-xs text-zinc-500">
          Building geocoding is in progress — the map will populate as buildings are enriched.
        </p>
      </div>
    );
  }

  if (!apiKey || loadError) {
    return <MapError message={!apiKey ? "Google Maps API key not configured." : "Google Maps failed to load."} />;
  }
  if (!isLoaded) {
    return <div className="w-full h-[360px] lg:h-[480px] rounded-2xl border border-white/10 bg-zinc-900/50 animate-pulse" />;
  }

  const center = geocoded.length === 1
    ? { lat: geocoded[0].latitude as number, lng: geocoded[0].longitude as number }
    : defaultCenter;
  const bounds = new window.google.maps.LatLngBounds();
  geocoded.forEach((result) => bounds.extend({ lat: result.latitude as number, lng: result.longitude as number }));

  return (
    <div className="relative h-[360px] lg:h-[480px]">
      <GoogleMap
        mapContainerStyle={containerStyle}
        center={center}
        zoom={geocoded.length === 1 ? 14 : 11}
        onLoad={(map) => {
          if (geocoded.length > 1) map.fitBounds(bounds, 48);
        }}
        options={{
          fullscreenControl: false,
          mapTypeControl: false,
          streetViewControl: false,
          styles: [
            { elementType: "geometry", stylers: [{ color: "#18181b" }] },
            { elementType: "labels.text.fill", stylers: [{ color: "#a1a1aa" }] },
            { elementType: "labels.text.stroke", stylers: [{ color: "#18181b" }] },
            { featureType: "road", elementType: "geometry", stylers: [{ color: "#27272a" }] },
            { featureType: "water", elementType: "geometry", stylers: [{ color: "#111827" }] },
          ],
        }}
      >
        {geocoded.map((result, index) => {
          const color = markerColor(result);
          const buildingSlug = result.building_name ? slugify(result.building_name) : null;
          const localitySlug = result.micro_market ? slugify(result.micro_market) : null;
          const href = buildingSlug ? `/buildings/${buildingSlug}` : localitySlug ? `/localities/${localitySlug}` : null;
          const parts = [result.bhk, result.furnishing, result.micro_market].filter(Boolean).join(" · ");
          return (
            <Marker
              key={`${result.id}-${index}`}
              position={{ lat: result.latitude as number, lng: result.longitude as number }}
              onClick={() => setSelected(result)}
              icon={{
                path: window.google.maps.SymbolPath.CIRCLE,
                scale: 8,
                fillColor: color,
                fillOpacity: 1,
                strokeColor: "#111827",
                strokeWeight: 2,
              }}
            >
              {selected === result && (
                <InfoWindow onCloseClick={() => setSelected(null)}>
                  <div className="max-w-[220px] font-sans text-zinc-900">
                    <div className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-zinc-500">
                      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} />
                      {result.intent || result.asset_type || "listing"}
                    </div>
                    <div className="mb-1 text-[13px] font-semibold">{formatBuildingName(result.building_name)}</div>
                    <div className="text-lg font-bold">{formatPrice(result.price, result.price_unit)}</div>
                    {parts && <div className="mt-1 text-[11px] text-zinc-500">{parts}</div>}
                    {href && <Link className="mt-2 inline-block text-[11px] font-semibold text-emerald-600" href={href}>View details →</Link>}
                  </div>
                </InfoWindow>
              )}
            </Marker>
          );
        })}
      </GoogleMap>
      <div className="absolute bottom-3 left-3 flex flex-wrap gap-1.5 text-[10px] text-zinc-400">
        <span className="inline-flex items-center gap-1 rounded-full bg-black/70 px-2 py-1 backdrop-blur"><span className="h-2 w-2 rounded-full bg-green-500" /> {geocoded.length} on map</span>
        {results.length > geocoded.length && <span className="inline-flex items-center gap-1 rounded-full bg-black/70 px-2 py-1 backdrop-blur">{results.length - geocoded.length} without coordinates</span>}
      </div>
    </div>
  );
}

function MapError({ message }: { message: string }) {
  return <div className="flex w-full h-[360px] lg:h-[480px] flex-col items-center justify-center gap-2 rounded-2xl border border-white/10 bg-zinc-900/60 px-6 text-center"><div className="text-2xl text-zinc-500">⌖</div><p className="text-sm text-zinc-400">{message}</p></div>;
}
