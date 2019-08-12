//
// Copyright (c) 2017-2019 the rbfx project.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.
//

#include "../Core/Context.h"
#include "../Engine/PluginApplication.h"
#include "../IO/Log.h"
#if !defined(URHO3D_STATIC) && defined(URHO3D_PLUGINS)
#   if !defined(NDEBUG) && defined(URHO3D_LOGGING)
#       define CR_DEBUG 1
#       define CR_ERROR(format, ...) URHO3D_LOGERRORF(format, ##__VA_ARGS__)
#       define CR_LOG(format, ...)   URHO3D_LOGTRACEF(format, ##__VA_ARGS__)
#       define CR_TRACE
#   endif
#   if DESKTOP
#       include <cr/cr.h>
#   endif
#endif

namespace Urho3D
{

PluginApplication::~PluginApplication()
{
    for (const auto& pair : registeredTypes_)
    {
        if (!pair.second.empty())
            context_->RemoveFactory(pair.first, pair.second.c_str());
        else
            context_->RemoveFactory(pair.first);
        context_->RemoveAllAttributes(pair.first);
        context_->RemoveSubsystem(pair.first);
    }
}

void PluginApplication::RecordPluginFactory(StringHash type, const char* category)
{
    registeredTypes_.push_back({type, category});
}

#if !defined(URHO3D_STATIC) && defined(URHO3D_PLUGINS)
int PluginApplication::PluginMain(void* ctx_, size_t operation, PluginApplication*(*factory)(Context*))
{
#if DESKTOP
    assert(ctx_);
    auto* ctx = static_cast<cr_plugin*>(ctx_);

    switch (operation)
    {
    case CR_LOAD:
    {
        auto* context = static_cast<Context*>(ctx->userdata);
        ctx->userdata = factory(context);
        return 0;
    }
    case CR_UNLOAD:
    case CR_CLOSE:
    {
        auto* application = static_cast<PluginApplication*>(ctx->userdata);
        ctx->userdata = application->GetContext();
        return 0;
    }
    case CR_STEP:
    {
        return 0;
    }
    default:
		break;
    }
	assert(false);
#endif
	return -3;
}
#endif

}
