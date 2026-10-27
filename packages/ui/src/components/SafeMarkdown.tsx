"use client";

import { useState } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import {
  allowedImageAttributes,
  blockedImageLabel,
  decideImage,
  markdownImagePolicy,
} from "@/lib/markdown-image-policy";

// Every Markdown sink renders through this wrapper so no caller can render a
// raw <img> from content: images always go through markdown-image-policy.
function MarkdownImage({ src, alt }: { src?: unknown; alt?: string }) {
  const [failed, setFailed] = useState(false);
  const decision = decideImage(src, markdownImagePolicy);
  const text = alt ?? "";
  if (!decision.allow) {
    return (
      <span role="img" aria-label={text || "imagine blocată"} data-md-image="blocked"
        data-md-reason={decision.reason} className="text-xs text-fg-muted italic">
        {blockedImageLabel(decision, text)}
      </span>
    );
  }
  if (failed) {
    return (
      <span role="img" aria-label={text || "imagine"} data-md-image="error" className="text-xs text-fg-muted italic">
        {`Imaginea nu a putut fi încărcată${text ? ` („${text}”)` : ""}.`}
      </span>
    );
  }
  // eslint-disable-next-line @next/next/no-img-element
  return <img {...allowedImageAttributes(decision)} alt={text} data-md-image="allowed" onError={() => setFailed(true)} />;
}

const img: Components["img"] = ({ src, alt }) => <MarkdownImage src={src} alt={alt} />;

export default function SafeMarkdown(props: Options) {
  return <ReactMarkdown {...props} components={{ ...props.components, img }} />;
}
