"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

// The home page is the chat. The page is plain client code so it also works as a
// static export; /chat does its own sign-in check, so nothing is decided here.
export default function HomePage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/chat/");
  }, [router]);
  return (
    <p className="p-6 text-center text-sm text-muted">
      Opening the chat… <Link href="/chat/" className="underline">Continue</Link>
    </p>
  );
}
