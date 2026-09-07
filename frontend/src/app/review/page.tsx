import type { Metadata } from "next";

import { ReviewQueue } from "@/components/review-queue";

export const metadata: Metadata = {
  title: "Review queue · LedgerDrop",
  description: "Invoices awaiting a human decision.",
};

export default function ReviewQueuePage() {
  return <ReviewQueue />;
}
