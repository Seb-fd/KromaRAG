import Image from "next/image";

/**
 * Shared KromaRAG brand mark — single `src`/sizing source so the login
 * card, sidebar, chat empty state and dev bypass can't drift apart.
 *
 * The artwork is a circular emblem on a square tile: always render it
 * circular (`rounded-full`, the default) so frame and art share one
 * geometry. Rings must be symmetric (see `.avatar-ring` in globals.css) —
 * offset drop shadows read as misaligned on circular marks.
 */
export function Logo({
  size = 32,
  className = "rounded-full",
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
