import type { Metadata } from "next";

import { ReviewDetail } from "@/components/review-detail";

export const metadata: Metadata = {
  title: "Review invoice · LedgerDrop",
  description: "Inspect an invoice and record an approve or reject decision.",
};

export default async function ReviewDetailPage({
  params,
}: {
  params: Promise<{ documentId: string }>;
}) {
  const { documentId } = await params;
  return <ReviewDetail documentId={documentId} />;
}
