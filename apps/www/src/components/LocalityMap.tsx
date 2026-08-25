"use client";

import { GoogleMap, InfoWindow, Marker, useJsApiLoader } from "@react-google-maps/api";
import { useMemo, useState } from "react";
import type { BuildingOnMap } from "@/lib/localities";

type Props = { locality: string; buildings: BuildingOnMap[]; apiKey: string | null };
type Point = { lat: number; lng: number };
const containerStyle = { width: "100%", height: "100%" };

function formatPrice(value: number | null, unit?: string | null): string {
  if (value == null) return "—";
  const u = (unit || "").toLowerCase();
  if (u === "m" || u === "million") return `AED ${value % 1 === 0 ? value : value.toFixed(2)}M`;
  if (u === "k" || u === "thousand") return `AED ${Math.round(value)}k`;
  return `AED ${Math.round(value).toLocaleString("en-AE")}`;
}

export default function LocalityMap({ locality, buildings, apiKey }: Props) {
  const [selected, setSelected] = useState<BuildingOnMap | null>(null);
  const geocoded = useMemo(() => buildings.filter((building) => building.latitude != null && building.longitude != null), [buildings]);
  const { isLoaded, loadError } = useJsApiLoader({ id: "propai-google-maps", googleMapsApiKey: apiKey || "" });

  if (geocoded.length === 0) return null;
  if (!apiKey || loadError) return <MapError message={!apiKey ? "Google Maps API key not configured." : "Google Maps failed to load."} />;
  if (!isLoaded) return <div className="w-full h-[280px] lg:h-[320px] rounded-xl border border-white/10 bg-zinc-900/50 animate-pulse" />;

  const center: Point = { lat: geocoded[0].latitude as number, lng: geocoded[0].longitude as number };
  const bounds = new window.google.maps.LatLngBounds();
  geocoded.forEach((building) => bounds.extend({ lat: building.latitude as number, lng: building.longitude as number }));

  return (
    <div className="w-full h-[280px] lg:h-[320px]">
      <GoogleMap
        mapContainerStyle={containerStyle}
        center={center}
        zoom={geocoded.length === 1 ? 14 : 11}
        onLoad={(map) => { if (geocoded.length > 1) map.fitBounds(bounds, 64); }}
        options={{ fullscreenControl: false, mapTypeControl: false, streetViewControl: false }}
      >
      {geocoded.map((building, index) => {
        const priceText = building.minPrice != null && building.maxPrice != null
          ? building.minPrice === building.maxPrice
            ? formatPrice(building.minPrice, building.priceUnit)
            : `${formatPrice(building.minPrice, building.priceUnit)} – ${formatPrice(building.maxPrice, building.priceUnit)}`
          : "Price on request";
        return (
          <Marker
            key={`${building.id ?? building.name}-${index}`}
            position={{ lat: building.latitude as number, lng: building.longitude as number }}
            onClick={() => setSelected(building)}
            icon={{ path: window.google.maps.SymbolPath.CIRCLE, scale: 8, fillColor: "#6B8E63", fillOpacity: 1, strokeColor: "#3F5A3A", strokeWeight: 2 }}
          >
            {selected === building && <InfoWindow onCloseClick={() => setSelected(null)}><div className="max-w-[220px] font-sans text-zinc-900"><h3 className="text-sm font-semibold">{building.name}</h3>{building.bhkRange && <p className="text-xs text-zinc-500">{building.bhkRange}</p>}<p className="text-base font-bold">{priceText}</p><p className="text-xs text-zinc-500">{building.listingCount} active listing{building.listingCount === 1 ? "" : "s"}</p></div></InfoWindow>}
          </Marker>
        );
      })}
      </GoogleMap>
    </div>
  );
}

function MapError({ message }: { message: string }) {
  return <div className="flex w-full h-[280px] lg:h-[320px] flex-col items-center justify-center gap-2 rounded-xl border border-white/10 bg-zinc-900/60 px-6 text-center"><div className="text-2xl text-zinc-500">⌖</div><p className="text-sm text-zinc-400">{message}</p><p className="text-xs text-zinc-600">Building listings below are still available without the map.</p></div>;
}
