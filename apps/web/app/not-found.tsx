import Link from "next/link";

export const metadata = {
  title: "404 — Page not found",
  description: "The page you are looking for does not exist.",
};

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-lg flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="font-display text-sm tracking-widest uppercase ink-faint">404</p>
      <h1 className="font-display text-2xl font-semibold tracking-tight">Page not found</h1>
      <p className="text-sm ink-soft">
        The page you asked for doesn&apos;t exist or was moved.
      </p>
      <Link
        href="/"
        className="kroma-gradient-btn mt-2 inline-flex h-10 items-center rounded-lg px-4 text-sm font-medium"
      >
        Back to app
      </Link>
    </main>
  );
}
