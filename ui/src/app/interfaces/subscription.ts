export interface SubscriptionRow {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  check_interval_minutes: number;
  download_type: string;
  codec: string;
  format: string;
  quality: string;
  folder: string;
  title_regex?: string;
  skip_subscriber_only?: boolean;
  clip_start?: number | null;
  clip_end?: number | null;
  /** 'feed' polls for new videos; 'live' records a live channel when it goes live. */
  kind?: 'feed' | 'live';
  live_categories?: string[];
  live_now?: boolean;
  live_title?: string;
  live_category?: string;
  /** Stop a recording once the stream has not matched for this long; 0 = never. */
  live_stop_after_minutes?: number;
  /** Check interval while one of this subscription's recordings runs. */
  live_recheck_minutes?: number;
  /** When the running recording's stream was first seen not matching (epoch seconds). */
  live_mismatch_since?: number | null;
  live_recording?: boolean;
  /** The channel's profile picture; empty when unknown. */
  avatar_url?: string;
  last_checked: number | null;
  seen_count: number;
  error: string | null;
}
