@extends('layouts.auth')
@section('title', 'Tài khoản')
@section('content')
<h1>{{ auth()->user()->name }}</h1><p class="lead">Tài khoản đã sẵn sàng để dùng chung cho website và diễn đàn.</p>
<dl class="profile"><div><dt>Email</dt><dd>{{ auth()->user()->email }}</dd></div><div><dt>Trạng thái</dt><dd>Đã xác minh</dd></div></dl>
<form method="POST" action="{{ route('logout') }}">@csrf<button class="secondary" type="submit">Đăng xuất</button></form>
@endsection
