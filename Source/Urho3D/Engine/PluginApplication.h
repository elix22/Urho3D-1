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

#pragma once

#include "../Core/Context.h"
#include "../Core/Object.h"


namespace Urho3D
{

/// Enumeration describing plugin file path status.
enum PluginType
{
    /// Not a valid plugin.
    PLUGIN_INVALID,
    /// A native plugin.
    PLUGIN_NATIVE,
    /// A managed plugin.
    PLUGIN_MANAGED,
};

/// Base class for creating plugins for Editor.
class URHO3D_API PluginApplication : public Object
{
    URHO3D_OBJECT(PluginApplication, Object);
public:
    /// Construct.
    explicit PluginApplication(Context* context) : Object(context) { }
    /// Destruct.
    ~PluginApplication() override;
    /// Called when plugin is being loaded. Register all custom components and subscribe to events here.
    virtual void Load() { }
    /// Called when application is started. May be called multiple times but no earlier than before next Stop() call.
    virtual void Start() { }
    /// Called when application is stopped.
    virtual void Stop() { }
    /// Called when plugin is being unloaded. Unregister all custom components and unsubscribe from events here.
    virtual void Unload() { }
    /// Register a factory for an object type.
    template<typename T> void RegisterFactory();
    /// Register a factory for an object type and specify the object category.
    template<typename T> void RegisterFactory(const char* category);
    /// Main function of native plugin.
    static int PluginMain(void* ctx_, size_t operation, PluginApplication*(*factory)(Context*));

protected:
    /// Record type factory that will be unregistered on plugin unload.
    void RecordPluginFactory(StringHash type, const char* category);

private:
    /// Types registered with the engine. They will be unloaded when plugin is reloaded.
    ea::vector<ea::pair<StringHash, ea::string>> registeredTypes_;
};

template<typename T>
void PluginApplication::RegisterFactory()
{
    context_->RegisterFactory<T>();
    RecordPluginFactory(T::GetTypeStatic(), nullptr);
}

template<typename T>
void PluginApplication::RegisterFactory(const char* category)
{
    context_->RegisterFactory<T>(category);
    RecordPluginFactory(T::GetTypeStatic(), category);
}

}

/// Macro for defining entry point of editor plugin.
#if defined(URHO3D_STATIC) || !defined(URHO3D_PLUGINS)
// In static builds user must manually initialize his plugins by creating plugin instance.
#define URHO3D_DEFINE_PLUGIN_MAIN(Class)
#else
#define URHO3D_DEFINE_PLUGIN_MAIN(Class)                                  \
    extern "C" URHO3D_EXPORT_API int cr_main(void* ctx, size_t operation) \
    {                                                                     \
        return Urho3D::PluginApplication::PluginMain(ctx, operation,      \
            [](Urho3D::Context* context) -> Urho3D::PluginApplication* {  \
                return new Class(context);                                \
            });                                                           \
    }
#endif
