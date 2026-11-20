-- Pickit 회원 정보 (profiles)
-- Supabase 대시보드 → SQL Editor → New query 에 이 파일 전체를 붙여 넣고 Run.
-- 여러 번 실행해도 괜찮게 작성함 (if not exists / or replace / drop ... if exists)

-- 1) profiles 표: 가입한 사람마다 한 줄
create table if not exists public.profiles (
  id         uuid primary key references auth.users (id) on delete cascade,
  email      text,
  role       text not null default 'user' check (role in ('user', 'admin')),
  created_at timestamptz not null default now()
);

-- 2) 가입하면 profiles에 자동으로 한 줄 만들기
--    security definer: 가입하는 사람 권한이 아니라 함수 주인 권한으로 실행 (RLS와 상관없이 넣을 수 있게)
--    search_path를 비워서 다른 스키마의 같은 이름 함수·표에 속지 않게 함
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- 이미 가입한 사람이 있으면 profiles에 채워 넣기
insert into public.profiles (id, email)
select id, email from auth.users
on conflict (id) do nothing;

-- 3) RLS(행 단위 보안): 로그인한 사람은 자기 행만 읽기·수정
alter table public.profiles enable row level security;

drop policy if exists "profiles: 본인만 읽기" on public.profiles;
create policy "profiles: 본인만 읽기"
  on public.profiles for select
  to authenticated
  using ((select auth.uid()) = id);

drop policy if exists "profiles: 본인만 수정" on public.profiles;
create policy "profiles: 본인만 수정"
  on public.profiles for update
  to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);
-- insert·delete 정책은 만들지 않음 → 화면에서는 행을 만들거나 지울 수 없음 (트리거·탈퇴 시 cascade만)

-- 4) role·id·created_at은 본인이 못 바꾸게: 화면(anon·authenticated)에는 email 열만 수정 권한
revoke insert, update, delete on public.profiles from anon, authenticated;
revoke select on public.profiles from anon;
grant select on public.profiles to authenticated;
grant update (email) on public.profiles to authenticated;

-- 관리자 지정은 SQL Editor(관리자 권한)에서만:
--   update public.profiles set role = 'admin' where email = '내이메일@example.com';


-- =====================================================================
-- 여기부터 추가분 (사용 기록·찜 목록). 위 부분을 이미 실행했어도 파일 전체를 다시 실행해도 됨
-- =====================================================================

-- 5) admin 확인 함수: RLS 정책 안에서 profiles를 읽을 때 쓰는 도우미
--    security definer라서 profiles의 RLS와 상관없이 "지금 로그인한 사람"의 role만 확인
create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles
    where id = (select auth.uid()) and role = 'admin'
  );
$$;
revoke execute on function public.is_admin() from public, anon;
grant execute on function public.is_admin() to authenticated;

-- 6) usage_logs: 새 검색·상담 사용 기록 (서버가 SERVICE 키로만 씀)
--    하루 사용 횟수 = 오늘(한국 시간) status='ok'인 줄 수
create table if not exists public.usage_logs (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  email       text,
  action      text not null check (action in ('search', 'chat', 'compare')),
  query       text check (char_length(query) <= 300),
  engine      text,
  status      text not null,          -- ok · cached · limited · cancelled · timeout · error
  duration_ms integer,                -- 응답 시간(밀리초)
  created_at  timestamptz not null default now()
);
-- 오늘 횟수를 빨리 세기 위한 색인
create index if not exists usage_logs_user_day
  on public.usage_logs (user_id, action, status, created_at desc);

alter table public.usage_logs enable row level security;

drop policy if exists "usage_logs: 본인 또는 admin 읽기" on public.usage_logs;
create policy "usage_logs: 본인 또는 admin 읽기"
  on public.usage_logs for select
  to authenticated
  using ((select auth.uid()) = user_id or (select public.is_admin()));
-- insert·update·delete 정책 없음 + 권한 회수 → 화면에서는 쓰기·수정·삭제 불가 (SERVICE 키는 RLS를 건너뜀)
revoke all on public.usage_logs from anon, authenticated;
grant select on public.usage_logs to authenticated;

-- 7) favorites: 찜 목록 (로그인한 사람의 브라우저가 supabase-js로 직접 읽고 씀)
create table if not exists public.favorites (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
  item_key   text not null check (char_length(item_key) between 1 and 300),   -- "브랜드 모델" (같은 상품 중복 방지)
  item       jsonb not null check (jsonb_typeof(item) = 'object' and pg_column_size(item) <= 8192),  -- 상품 정보
  query      text check (char_length(query) <= 300),                           -- 찜할 때의 검색어
  created_at timestamptz not null default now(),
  unique (user_id, item_key)
);

alter table public.favorites enable row level security;

drop policy if exists "favorites: 본인만 읽기" on public.favorites;
create policy "favorites: 본인만 읽기"
  on public.favorites for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "favorites: 본인만 추가" on public.favorites;
create policy "favorites: 본인만 추가"
  on public.favorites for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "favorites: 본인만 삭제" on public.favorites;
create policy "favorites: 본인만 삭제"
  on public.favorites for delete
  to authenticated
  using ((select auth.uid()) = user_id);

revoke all on public.favorites from anon, authenticated;
grant select, insert, delete on public.favorites to authenticated;


-- =====================================================================
-- 추가분 2: 서버 전체 새 검색 상한 · 찜 보호 강화 (파일 전체를 다시 실행해도 됨)
-- =====================================================================

-- 8) usage_logs.exempt: admin·내 PC 사용 표시 (서버 전체 하루 상한 GLOBAL_SEARCH_LIMIT에 안 셈)
alter table public.usage_logs add column if not exists exempt boolean not null default false;
create index if not exists usage_logs_day
  on public.usage_logs (action, status, created_at desc) where not exempt;

-- 9) favorites: 화면에서 넣을 수 있는 열을 제한
--    user_id는 RLS가 "본인"만 허용하고, created_at은 서버 시각(now())만 쓰게 함
revoke insert on public.favorites from authenticated;
grant insert (user_id, item_key, item, query) on public.favorites to authenticated;


-- =====================================================================
-- 추가분 3: 관리자 대시보드 (파일 전체를 다시 실행해도 됨)
-- =====================================================================

-- 10) usage_logs.tokens: Gemini 응답에 사용 토큰 수가 있으면 저장 (Claude·캐시 등은 비어 있음)
alter table public.usage_logs add column if not exists tokens integer check (tokens is null or tokens >= 0);
-- 대시보드가 기간별로 최신순으로 읽을 때 쓰는 색인
create index if not exists usage_logs_created on public.usage_logs (created_at desc);
