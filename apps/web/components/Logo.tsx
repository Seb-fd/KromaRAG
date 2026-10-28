import Image from "next/image";

/**
 * Shared KromaRAG brand mark — single `src`/sizing source so the login
 * card, sidebar, chat empty state and dev bypass can't drift apart.
 */
export function Logo({
  size = 32,
  className = "rounded-lg",
}: {
  size?: number;
  className?: string;
}) {
  return (
    <Image
      alt="KromaRAG Logo"
      className={className}
      height={size}
      priority
      unoptimized
      src="/logo.webp"
      width={size}
    />
  );
}
