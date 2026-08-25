"use client";

import { useEffect, useRef, useState } from "react";

interface CountUpProps {
  end: number;
  duration?: number;
  prefix?: string;
  suffix?: string;
  className?: string;
  locale?: string;
}

export default function CountUp({
  end,
  duration = 1500,
  prefix = "",
  suffix = "",
  className = "",
  locale = "en-AE",
}: CountUpProps) {
  // SSR: render the real value so crawlers see accurate numbers.
  // On mount, reset to 0 and animate up — users see the count-up effect.
  const [count, setCount] = useState(end);
  const [hasMounted, setHasMounted] = useState(false);
  const [isVisible, setIsVisible] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setHasMounted(true);
    setCount(0);
  }, []);

  useEffect(() => {
    if (!hasMounted) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true);
          observer.disconnect();
        }
      },
      { threshold: 0.1, rootMargin: "50px" }
    );

    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, [hasMounted]);

  useEffect(() => {
    if (!isVisible) return;

    let startTime: number | null = null;
    let frameId: number;

    const animate = (timestamp: number) => {
      if (!startTime) startTime = timestamp;
      const progress = Math.min((timestamp - startTime) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
      const current = Math.floor(eased * end);
      setCount(current);

      if (progress < 1) {
        frameId = requestAnimationFrame(animate);
      }
    };

    frameId = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frameId);
  }, [isVisible, duration, end]);

  return (
    <div ref={ref} className={className}>
      {prefix}
      {count.toLocaleString(locale)}
      {suffix}
    </div>
  );
}