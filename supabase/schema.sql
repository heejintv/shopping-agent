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
