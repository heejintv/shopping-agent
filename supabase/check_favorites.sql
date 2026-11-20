-- 찜(favorites)이 계정끼리 섞였는지 확인하는 SQL
-- Supabase 대시보드 → SQL Editor 에서 한 덩어리씩 골라 실행 (읽기만 함. 맨 아래 삭제문은 주석 상태)

-- 1) 계정별 찜 개수
select f.user_id, p.email, count(*) as 찜_개수,
       min(f.created_at) as 처음_찜, max(f.created_at) as 마지막_찜
from public.favorites f
left join public.profiles p on p.id = f.user_id
group by f.user_id, p.email
order by 찜_개수 desc;

-- 2) 같은 상품이 여러 계정에 들어 있는 경우 (섞였을 가능성. 진짜로 둘 다 찜했을 수도 있음)
select f.item_key, count(distinct f.user_id) as 계정_수,
       array_agg(distinct p.email) as 이메일들
from public.favorites f
left join public.profiles p on p.id = f.user_id
group by f.item_key
having count(distinct f.user_id) > 1
order by 계정_수 desc;

-- 3) 가입하기 "전" 시각으로 저장된 찜 = 예전 자동 합치기로 들어온 행일 가능성이 큼
--    (예전 코드는 브라우저에 있던 찜의 저장 시각을 그대로 넣었음)
select p.email, f.item_key, f.query, f.created_at as 찜_시각, u.created_at as 가입_시각
from public.favorites f
join auth.users u on u.id = f.user_id
left join public.profiles p on p.id = f.user_id
where f.created_at < u.created_at
order by p.email, f.created_at;

-- 4) 특정 계정의 찜 전체 보기 (이메일을 바꿔서 실행)
select f.id, f.item_key, f.query, f.created_at, f.item ->> 'price' as 가격
from public.favorites f
join public.profiles p on p.id = f.user_id
where p.email = 'b계정@example.com'
order by f.created_at desc;

-- 5) 확인 후 잘못 들어간 행 지우기 (주석을 풀고 이메일·item_key를 바꿔서 실행)
-- delete from public.favorites
-- where user_id = (select id from public.profiles where email = 'b계정@example.com')
--   and item_key in ('브랜드 모델1', '브랜드 모델2');
--
-- 3)번 결과(가입 전 시각 행)를 한꺼번에 지우려면:
-- delete from public.favorites f
-- using auth.users u
-- where u.id = f.user_id and f.created_at < u.created_at;
