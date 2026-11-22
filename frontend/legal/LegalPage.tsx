import React from 'react';
import { LanguageSwitch } from '@/i18n';

type Kind = 'privacy' | 'terms';

const CONTACT = 'zzxjl1@hotmail.com';

export const LegalPage: React.FC<{ kind: Kind }> = ({ kind }) => {
  const privacy = kind === 'privacy';
  return (
    <div className="min-h-screen bg-claude-bg px-6 py-12 text-claude-text">
      <div className="mx-auto max-w-2xl">
        <div className="mb-10 flex items-start justify-between gap-4">
          <a href="/" className="font-serif text-xl font-bold tracking-tight">Video Voice Translator</a>
          <LanguageSwitch />
        </div>

        <article className="rounded-3xl border border-[#e5e5e0] bg-white p-7 shadow-sm sm:p-10">
          <p className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-claude-accent">Draft</p>
          <h1 className="font-serif text-3xl font-bold">{privacy ? 'Privacy Policy' : 'Terms of Service'}</h1>
          <p className="mt-2 text-[12px] text-gray-400">Last updated: November 2026</p>

          {privacy ? (
            <div className="mt-8 space-y-5 text-[14px] leading-relaxed text-gray-600">
              <p>
                Video Voice Translator lets you upload a video, transcribe and translate its speech,
                generate dubbing, and export the result. This page explains what we store and why.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">Information we collect</h2>
              <p>
                When you sign in with Google, we receive your Google account ID, name, email address,
                email verification status, and profile picture. We do not receive or store your Google
                password. We do not request access to Gmail, Drive, YouTube, or other Google content.
              </p>
              <p>
                We store the videos, audio, transcripts, translations, and exports you create so that
                you can return to them. We also store basic session data (a random session identifier,
                timestamps, and optionally browser/device details) to keep you signed in.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">How we use it</h2>
              <p>
                We use this information to authenticate you, associate projects with your account,
                operate the translation and dubbing pipeline, and protect the service from abuse.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">Cookies</h2>
              <p>
                We use a strictly necessary HttpOnly session cookie to keep you signed in. It is not
                used for advertising or cross-site tracking.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">Retention and deletion</h2>
              <p>
                Projects remain available until you delete them or ask us to remove your account.
                Account deletion is currently handled manually; contact us at {CONTACT}.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">Contact</h2>
              <p>Questions or requests: <a className="text-claude-accent underline" href={`mailto:${CONTACT}`}>{CONTACT}</a></p>
            </div>
          ) : (
            <div className="mt-8 space-y-5 text-[14px] leading-relaxed text-gray-600">
              <p>
                By using Video Voice Translator, you agree to use the service only for content you have
                the right to upload and process. You are responsible for the videos, audio, and text you
                submit, and for complying with applicable copyright, privacy, and publicity rules.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">Accounts</h2>
              <p>
                Access is provided through Google sign-in. Keep access to your Google account secure.
                Do not share your session or attempt to access another user's projects.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">Service and content</h2>
              <p>
                The service is provided as-is. We may change, suspend, or discontinue features. You retain
                ownership of your content; we process it only to provide the service.
              </p>
              <h2 className="pt-2 text-[16px] font-bold text-gray-800">Contact</h2>
              <p>Questions: <a className="text-claude-accent underline" href={`mailto:${CONTACT}`}>{CONTACT}</a></p>
            </div>
          )}
        </article>

        <div className="mt-6 flex gap-4 text-[12px] text-gray-400">
          <a className="hover:text-claude-accent" href="/privacy">Privacy</a>
          <a className="hover:text-claude-accent" href="/terms">Terms</a>
          <a className="hover:text-claude-accent" href="/">Back to app</a>
        </div>
      </div>
    </div>
  );
};
