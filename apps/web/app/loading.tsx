export default function Loading() {
  return (
    <main
      aria-busy="true"
      aria-label="Loading"
      className="mx-auto flex min-h-dvh w-full max-w-3xl flex-col gap-3 px-6 py-10"
    >
      <div className="h-11 rounded-xl border border-[var(--border)] bg-[var(--surface-solid)]" />
      <div className="h-24 animate-pulse rounded-2xl bg-[var(--surface-2)]" />
      <div className="h-24 animate-pulse rounded-2xl bg-[var(--surface-2)]" />
      <div className="h-24 animate-pulse rounded-2xl bg-[var(--surface-2)]" />
    </main>
  );
}
