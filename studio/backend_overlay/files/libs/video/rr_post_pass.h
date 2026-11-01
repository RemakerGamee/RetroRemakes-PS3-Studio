/* RetroRemakes PS3 Studio - backend fixes
 * Copyright (c) 2026 RetroRemakes. All rights reserved.
 * Parts of this file come from open-source projects credited in NOTICE.md
 * (ps3recomp, RPCS3 and others) and remain under their original licenses.
 */
/* Final image post pass for native builds: edge anti-aliasing (RR_AA=fxaa),
 * adaptive sharpening (RR_SHARPEN=1..100) and output dithering (RR_DITHER=1).
 * Runs on the finished backbuffer just before it is presented; off by default.
 * Included once from rsx_d3d12_backend.c (needs s_d3d). */
#ifndef RR_POST_PASS_H
#define RR_POST_PASS_H

static const char rr_post_hlsl[] =
"Texture2D t:register(t0); SamplerState s:register(s0);\n"
"cbuffer c:register(b0){ float2 rcp; float sharp; float flags; };\n"
"struct V{ float4 p:SV_POSITION; float2 uv:TEXCOORD; };\n"
"V vs(uint id:SV_VertexID){ V o; float2 uv=float2((id<<1)&2, id&2); o.uv=uv;\n"
"  o.p=float4(uv*float2(2,-2)+float2(-1,1),0,1); return o; }\n"
"float L(float3 c){ return dot(c,float3(0.299,0.587,0.114)); }\n"
"float3 T(float2 uv){ return t.SampleLevel(s,uv,0).rgb; }\n"
"float3 edgeaa(float2 uv, float3 c){\n"
"  float lm=L(c), ln=L(T(uv+float2(0,-rcp.y))), ls=L(T(uv+float2(0,rcp.y)));\n"
"  float le=L(T(uv+float2(rcp.x,0))), lw=L(T(uv+float2(-rcp.x,0)));\n"
"  float mn=min(lm,min(min(ln,ls),min(le,lw))), mx=max(lm,max(max(ln,ls),max(le,lw)));\n"
"  float range=mx-mn; if(range<max(0.0312,mx*0.125)) return c;\n"
"  float lnw=L(T(uv+rcp*float2(-1,-1))), lne=L(T(uv+rcp*float2(1,-1)));\n"
"  float lsw=L(T(uv+rcp*float2(-1,1))),  lse=L(T(uv+rcp*float2(1,1)));\n"
"  float eh=abs(lnw+lne-2*ln)+2*abs(lw+le-2*lm)+abs(lsw+lse-2*ls);\n"
"  float ev=abs(lnw+lsw-2*lw)+2*abs(ln+ls-2*lm)+abs(lne+lse-2*le);\n"
"  bool horz=eh>=ev; float l1=horz?ln:lw, l2=horz?ls:le;\n"
"  float g1=abs(l1-lm), g2=abs(l2-lm); bool neg=g1>=g2;\n"
"  float stepl=(horz?rcp.y:rcp.x)*(neg?-1:1);\n"
"  float ledge=0.5*(lm+(neg?l1:l2)); float gs=0.25*max(g1,g2);\n"
"  float2 perp=horz?float2(0,stepl):float2(stepl,0); float2 along=horz?float2(rcp.x,0):float2(0,rcp.y);\n"
"  float2 pb=uv+perp*0.5; float2 pn=pb-along, pp=pb+along;\n"
"  float en=L(T(pn))-ledge, ep=L(T(pp))-ledge; bool dn=abs(en)>=gs, dp=abs(ep)>=gs;\n"
"  static const float st[7]={1,1,1.5,2,2,4,8};\n"
"  [unroll] for(int i=0;i<7;i++){\n"
"    if(!dn){ pn-=along*st[i]; en=L(T(pn))-ledge; dn=abs(en)>=gs; }\n"
"    if(!dp){ pp+=along*st[i]; ep=L(T(pp))-ledge; dp=abs(ep)>=gs; } }\n"
"  float dN=horz?(uv.x-pn.x):(uv.y-pn.y), dP=horz?(pp.x-uv.x):(pp.y-uv.y);\n"
"  bool nearN=dN<dP; float dist=min(dN,dP), len=dN+dP;\n"
"  bool ok=((lm-ledge)<0)!=((nearN?en:ep)<0);\n"
"  float off=ok?(0.5-dist/len):0;\n"
"  float avg=(2*(ln+ls+le+lw)+lnw+lne+lsw+lse)/12; float sub=saturate(abs(avg-lm)/range);\n"
"  sub=smoothstep(0,1,sub); sub=sub*sub*0.75;\n"
"  return T(uv+perp*max(off,sub)); }\n"
"float4 ps(V i):SV_TARGET{ uint f=(uint)flags; float2 uv=i.uv; float3 c=T(uv);\n"
"  if(f&1u) c=edgeaa(uv,c);\n"
"  if(sharp>0){ float3 n=T(uv+float2(0,-rcp.y)), so=T(uv+float2(0,rcp.y)), e=T(uv+float2(rcp.x,0)), w=T(uv+float2(-rcp.x,0));\n"
"    float3 mn=min(c,min(min(n,so),min(e,w))), mx=max(c,max(max(n,so),max(e,w)));\n"
"    float3 amp=sqrt(saturate(min(mn,1-mx)/max(mx,1e-4)));\n"
"    float3 wt=-amp*lerp(0.125,0.2,sharp);\n"
"    c=saturate((c+(n+so+e+w)*wt)/(1+4*wt)); }\n"
"  if(f&2u){ float r=frac(52.9829189*frac(dot(i.p.xy,float2(0.06711056,0.00583715)))); c+=(r-0.5)/255.0; }\n"
"  return float4(c,1); }\n";

static struct {
    int tried, ok;
    ID3D12RootSignature* rs;
    ID3D12PipelineState* pso;
    ID3D12DescriptorHeap* heap;
    ID3D12Resource* scratch;
    u32 w, h;
} s_rr_post;

static int rr_post_init(void)
{
    s_rr_post.tried = 1;
    HRESULT hr;
    D3D12_DESCRIPTOR_RANGE range = {0};
    range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV; range.NumDescriptors = 1;
    D3D12_ROOT_PARAMETER rp[2] = {0};
    rp[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    rp[0].Constants.Num32BitValues = 4; rp[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    rp[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    rp[1].DescriptorTable.NumDescriptorRanges = 1; rp[1].DescriptorTable.pDescriptorRanges = &range;
    rp[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    D3D12_STATIC_SAMPLER_DESC sd = {0};
    sd.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sd.MaxLOD = D3D12_FLOAT32_MAX; sd.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    D3D12_ROOT_SIGNATURE_DESC rd = {0};
    rd.NumParameters = 2; rd.pParameters = rp; rd.NumStaticSamplers = 1; rd.pStaticSamplers = &sd;
    ID3DBlob *sig = NULL, *err = NULL, *vb = NULL, *pb = NULL;
    hr = D3D12SerializeRootSignature(&rd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err);
    if (FAILED(hr)) goto fail;
    hr = s_d3d.device->lpVtbl->CreateRootSignature(s_d3d.device, 0, sig->lpVtbl->GetBufferPointer(sig),
            sig->lpVtbl->GetBufferSize(sig), &IID_ID3D12RootSignature, (void**)&s_rr_post.rs);
    if (FAILED(hr)) goto fail;
    if (err) { err->lpVtbl->Release(err); err = NULL; }
    hr = D3DCompile(rr_post_hlsl, sizeof(rr_post_hlsl) - 1, "rr_post", NULL, NULL, "vs", "vs_5_0", 0, 0, &vb, &err);
    if (FAILED(hr)) goto fail;
    hr = D3DCompile(rr_post_hlsl, sizeof(rr_post_hlsl) - 1, "rr_post", NULL, NULL, "ps", "ps_5_0", 0, 0, &pb, &err);
    if (FAILED(hr)) goto fail;
    {
        D3D12_GRAPHICS_PIPELINE_STATE_DESC pd = {0};
        pd.pRootSignature = s_rr_post.rs;
        pd.VS.pShaderBytecode = vb->lpVtbl->GetBufferPointer(vb); pd.VS.BytecodeLength = vb->lpVtbl->GetBufferSize(vb);
        pd.PS.pShaderBytecode = pb->lpVtbl->GetBufferPointer(pb); pd.PS.BytecodeLength = pb->lpVtbl->GetBufferSize(pb);
        pd.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
        pd.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
        pd.RasterizerState.DepthClipEnable = TRUE;
        pd.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
        pd.SampleMask = UINT_MAX;
        pd.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
        pd.NumRenderTargets = 1; pd.RTVFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM;
        pd.SampleDesc.Count = 1;
        hr = s_d3d.device->lpVtbl->CreateGraphicsPipelineState(s_d3d.device, &pd,
                &IID_ID3D12PipelineState, (void**)&s_rr_post.pso);
        if (FAILED(hr)) goto fail;
    }
    {
        D3D12_DESCRIPTOR_HEAP_DESC hd = {0};
        hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV; hd.NumDescriptors = 1;
        hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
        hr = s_d3d.device->lpVtbl->CreateDescriptorHeap(s_d3d.device, &hd,
                &IID_ID3D12DescriptorHeap, (void**)&s_rr_post.heap);
        if (FAILED(hr)) goto fail;
    }
    if (sig) sig->lpVtbl->Release(sig);
    vb->lpVtbl->Release(vb); pb->lpVtbl->Release(pb);
    s_rr_post.ok = 1;
    printf("[RR-DISPLAY] post pass ready\n");
    return 1;
fail:
    printf("[RR-DISPLAY] post pass unavailable (0x%08lX): %s\n", (unsigned long)hr,
           err ? (const char*)err->lpVtbl->GetBufferPointer(err) : "");
    if (err) err->lpVtbl->Release(err);
    if (sig) sig->lpVtbl->Release(sig);
    if (vb) vb->lpVtbl->Release(vb);
    if (pb) pb->lpVtbl->Release(pb);
    return 0;
}

static int rr_post_scratch(u32 w, u32 h)
{
    if (s_rr_post.scratch && s_rr_post.w == w && s_rr_post.h == h) return 1;
    if (s_rr_post.scratch) { s_rr_post.scratch->lpVtbl->Release(s_rr_post.scratch); s_rr_post.scratch = NULL; }
    D3D12_HEAP_PROPERTIES hp = {0}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC d = {0};
    d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; d.Width = w; d.Height = h;
    d.DepthOrArraySize = 1; d.MipLevels = 1; d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    d.SampleDesc.Count = 1; d.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    if (FAILED(s_d3d.device->lpVtbl->CreateCommittedResource(s_d3d.device, &hp, D3D12_HEAP_FLAG_NONE, &d,
            D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE, NULL, &IID_ID3D12Resource, (void**)&s_rr_post.scratch)))
        return 0;
    s_rr_post.w = w; s_rr_post.h = h;
    D3D12_SHADER_RESOURCE_VIEW_DESC v = {0};
    v.Format = DXGI_FORMAT_R8G8B8A8_UNORM; v.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    v.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING; v.Texture2D.MipLevels = 1;
    D3D12_CPU_DESCRIPTOR_HANDLE ch;
    s_rr_post.heap->lpVtbl->GetCPUDescriptorHandleForHeapStart(s_rr_post.heap, &ch);
    s_d3d.device->lpVtbl->CreateShaderResourceView(s_d3d.device, s_rr_post.scratch, &v, ch);
    return 1;
}

/* Backbuffer fi must be in RENDER_TARGET state; it is left in RENDER_TARGET. */
static void rr_post_pass(u32 fi)
{
    const rr_display_options* o = rr_display_options_get();
    if (!o->aa && !o->sharpen && !o->dither) return;
    if (!s_rr_post.tried) rr_post_init();
    if (!s_rr_post.ok || !s_d3d.width || !s_d3d.height) return;
    if (!rr_post_scratch(s_d3d.width, s_d3d.height)) return;
    ID3D12GraphicsCommandList* cl = s_d3d.cmd_list;
    D3D12_RESOURCE_BARRIER b[2] = {0};
    b[0].Type = b[1].Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b[0].Transition.Subresource = b[1].Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b[0].Transition.pResource = s_d3d.render_targets[fi];
    b[0].Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
    b[0].Transition.StateAfter  = D3D12_RESOURCE_STATE_COPY_SOURCE;
    b[1].Transition.pResource = s_rr_post.scratch;
    b[1].Transition.StateBefore = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    b[1].Transition.StateAfter  = D3D12_RESOURCE_STATE_COPY_DEST;
    cl->lpVtbl->ResourceBarrier(cl, 2, b);
    D3D12_TEXTURE_COPY_LOCATION dl = {0}, sl = {0};
    dl.pResource = s_rr_post.scratch; dl.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    sl.pResource = s_d3d.render_targets[fi]; sl.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_BOX box = {0, 0, 0, s_d3d.width, s_d3d.height, 1};
    cl->lpVtbl->CopyTextureRegion(cl, &dl, 0, 0, 0, &sl, &box);
    b[0].Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
    b[0].Transition.StateAfter  = D3D12_RESOURCE_STATE_RENDER_TARGET;
    b[1].Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    b[1].Transition.StateAfter  = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    cl->lpVtbl->ResourceBarrier(cl, 2, b);

    D3D12_CPU_DESCRIPTOR_HANDLE rtv;
    s_d3d.rtv_heap->lpVtbl->GetCPUDescriptorHandleForHeapStart(s_d3d.rtv_heap, &rtv);
    rtv.ptr += fi * s_d3d.rtv_descriptor_size;
    cl->lpVtbl->OMSetRenderTargets(cl, 1, &rtv, FALSE, NULL);
    D3D12_VIEWPORT vp = {0, 0, (float)s_d3d.width, (float)s_d3d.height, 0.0f, 1.0f};
    D3D12_RECT sc = {0, 0, (LONG)s_d3d.width, (LONG)s_d3d.height};
    cl->lpVtbl->RSSetViewports(cl, 1, &vp);
    cl->lpVtbl->RSSetScissorRects(cl, 1, &sc);
    cl->lpVtbl->SetPipelineState(cl, s_rr_post.pso);
    cl->lpVtbl->SetGraphicsRootSignature(cl, s_rr_post.rs);
    ID3D12DescriptorHeap* heaps[] = { s_rr_post.heap };
    cl->lpVtbl->SetDescriptorHeaps(cl, 1, heaps);
    D3D12_GPU_DESCRIPTOR_HANDLE gh;
    s_rr_post.heap->lpVtbl->GetGPUDescriptorHandleForHeapStart(s_rr_post.heap, &gh);
    cl->lpVtbl->SetGraphicsRootDescriptorTable(cl, 1, gh);
    float k[4] = { 1.0f / (float)s_d3d.width, 1.0f / (float)s_d3d.height,
                   (float)o->sharpen / 100.0f, (float)((o->aa ? 1 : 0) | (o->dither ? 2 : 0)) };
    cl->lpVtbl->SetGraphicsRoot32BitConstants(cl, 0, 4, k, 0);
    cl->lpVtbl->IASetPrimitiveTopology(cl, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    cl->lpVtbl->DrawInstanced(cl, 3, 1, 0, 0);
}

#endif
